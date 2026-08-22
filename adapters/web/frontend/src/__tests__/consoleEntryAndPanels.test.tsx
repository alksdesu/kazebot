// QQ 分区入口与三个上下文右栏的注册回归。
// QQ 那十页此前是独立视图，只能靠 ?view=console 深链进入；三个右栏组件实现完整却从未被注册。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Header, Sidebar } from '../components/layout';
import { SettingsRightPanel } from '../components/settings/SettingsRightPanel';
import { SettingsSidebar } from '../components/settings/SettingsSidebar';
import { SystemSettingsPage } from '../components/settings/pages/SystemSettingsPage';
import { settingsTabs } from '../components/settings/settingsTabs';
import { useChatStore, type ConversationMeta } from '../store/chatStore';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';
import { tabForLegacyDomain, useViewStore } from '../store/viewStore';
import type { AdminApproval } from '../api/supervisorClient';

const conversation: ConversationMeta = {
  id: 'conv-1',
  sessionId: 'sess-abcdef',
  title: '父对话',
  updatedAt: '2026-08-19T00:00:00.000Z',
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function renderSidebar() {
  return render(
    <Sidebar
      activeConversationId="conv-1"
      conversations={[conversation]}
      onCreateConversation={() => undefined}
      onDeleteConversation={() => undefined}
      onSelectConversation={() => undefined}
    />,
  );
}

describe('QQ 分区入口', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/');
    useViewStore.setState({ viewMode: 'chat', activeSettingsTab: 'general' });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('QQ 那十页和别的设置分区平铺在同一栏里，点一下就到', () => {
    render(<SettingsSidebar />);

    fireEvent.click(screen.getByRole('button', { name: '权限' }));

    expect(useViewStore.getState().activeSettingsTab).toBe('qq-permissions');
  });

  it('分组标题把 QQ 页和引擎页分开', () => {
    render(<SettingsSidebar />);

    expect(screen.getByText('QQ 机器人')).toBeInTheDocument();
    expect(screen.getByText('模型与渠道')).toBeInTheDocument();
  });

  it('两个「运行」不再撞：诊断页和链路页各是各的', () => {
    render(<SettingsSidebar />);

    fireEvent.click(screen.getByRole('button', { name: '运行' }));
    expect(useViewStore.getState().activeSettingsTab).toBe('runtime');

    fireEvent.click(screen.getByRole('button', { name: '链路' }));
    expect(useViewStore.getState().activeSettingsTab).toBe('qq-link');
  });

  it('聊天侧栏只留设置入口', () => {
    renderSidebar();

    expect(screen.queryByRole('button', { name: 'QQ 控制台' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    expect(useViewStore.getState().viewMode).toBe('settings');
  });
});

describe('视图与地址栏同步', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/');
    useViewStore.setState({ viewMode: 'chat', activeSettingsTab: 'general' });
  });

  afterEach(() => {
    cleanup();
    window.history.replaceState(null, '', '/');
  });

  it('退出后地址栏不再残留 view，刷新不会弹回设置', () => {
    window.history.replaceState(null, '', '/?view=settings&tab=qq-timing');

    useViewStore.getState().closeSettings();

    const params = new URLSearchParams(window.location.search);
    expect(params.get('view')).toBeNull();
    expect(params.get('tab')).toBeNull();
  });

  it('设置页把当前分区一并写进地址栏', () => {
    useViewStore.getState().openSettings('tools');
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('tools');

    useViewStore.getState().setSettingsTab('skills');
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('skills');
  });

  it('QQ 分区跟别的分区一样写 tab，没有第二套参数', () => {
    useViewStore.getState().openSettings('qq-permissions');

    const params = new URLSearchParams(window.location.search);
    expect(params.get('tab')).toBe('qq-permissions');
    expect(params.get('domain')).toBeNull();
  });

  it('不动其他人的 query 参数，但会清掉废弃的 domain', () => {
    window.history.replaceState(null, '', '/?token=abc&domain=timing');

    useViewStore.getState().openSettings('qq-timing');

    const params = new URLSearchParams(window.location.search);
    expect(params.get('token')).toBe('abc');
    // ?domain= 是控制台时代的定位参数，现在由 ?tab= 承担，留着会误导。
    expect(params.get('domain')).toBeNull();
  });
});

// 书签和聊天记录里还躺着 ?view=console&domain=x 的老链接。
describe('老控制台链接', () => {
  it('域名换算成对应的设置分区', () => {
    expect(tabForLegacyDomain('timing')).toBe('qq-timing');
    expect(tabForLegacyDomain('stickers')).toBe('qq-stickers');
  });

  it('「运行」不能直译：设置页已经有一个 runtime 了', () => {
    expect(tabForLegacyDomain('runtime')).toBe('qq-link');
    expect(settingsTabs.filter(tab => tab.id === 'runtime')).toHaveLength(1);
  });

  it('换算出来的每个 id 都真的注册过', () => {
    const ids = new Set(settingsTabs.map(tab => tab.id));
    for (const domain of ['account', 'channels', 'timing', 'permissions', 'persona',
      'providers', 'models', 'runtime', 'memory', 'stickers']) {
      expect(ids.has(tabForLegacyDomain(domain))).toBe(true);
    }
  });
});

describe('设置页上下文右栏', () => {
  beforeEach(() => {
    localStorage.clear();
    useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
    useSettingsSelectionStore.setState({ selectedApproval: null, advancedFile: 'runtime', systemLogs: [] });
    useChatStore.setState({ conversations: [], activeConversationId: null });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('审批页选中条目后右栏展示完整参数', () => {
    const approval: AdminApproval = {
      approval_id: 'apr-1',
      operation: 'write_file',
      tool_call_id: 'call-9',
      node_id: 'qq.orchestrator',
      details: { path: '/tmp/a.txt' },
    };
    useViewStore.setState({ activeSettingsTab: 'approvals' });
    useSettingsSelectionStore.setState({ selectedApproval: approval });

    render(<SettingsRightPanel />);

    expect(screen.getByText('审批详情')).toBeInTheDocument();
    expect(screen.getByText('apr-1')).toBeInTheDocument();
    expect(screen.getByText('qq.orchestrator')).toBeInTheDocument();
  });

  it('高级页右栏跟随当前编辑的文件切换说明', () => {
    useViewStore.setState({ activeSettingsTab: 'advanced' });

    const view = render(<SettingsRightPanel />);
    expect(screen.getByText(/Runtime Config 控制引擎运行参数/)).toBeInTheDocument();
    view.unmount();

    useSettingsSelectionStore.setState({ advancedFile: 'policy' });
    render(<SettingsRightPanel />);
    expect(screen.getByText(/Policy 控制工具调用/)).toBeInTheDocument();
  });

  it('系统页的重载操作会写进右栏日志', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/v1/config/reload')) return jsonResponse({ ok: true });
      if (url.endsWith('/v1/admin/state')) return jsonResponse({ sessions: 0, pending_approvals: [], engine_runtime: {} });
      return jsonResponse({ status: 'ok', uptime_seconds: 1 });
    });
    vi.stubGlobal('fetch', fetchMock);
    useViewStore.setState({ activeSettingsTab: 'system' });

    render(<SystemSettingsPage />);
    fireEvent.click(await screen.findByRole('button', { name: '重载配置' }));

    await waitFor(() => {
      expect(useSettingsSelectionStore.getState().systemLogs).toHaveLength(1);
    });
    expect(useSettingsSelectionStore.getState().systemLogs[0]).toContain('配置已重载');

    cleanup();
    render(<SettingsRightPanel />);
    expect(screen.getByText('最近操作日志')).toBeInTheDocument();
    expect(screen.getByText(/配置已重载/)).toBeInTheDocument();
  });

  it('系统页右栏在无操作记录时给出空态而不是空白', () => {
    useViewStore.setState({ activeSettingsTab: 'system' });

    render(<SettingsRightPanel />);

    expect(screen.getByText('暂无配置重载或重启操作记录。')).toBeInTheDocument();
  });
});

// 设置里那个只读的「模型」页删掉后，会话级模型覆盖只剩顶栏模型标签一个入口。
describe('会话模型覆盖的入口与去向', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
    fetchMock = vi.fn(async () => jsonResponse({}));
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function renderHeader(sessionId: string) {
    return render(
      <Header
        isGenerating={false}
        onCancel={() => undefined}
        onReset={() => undefined}
        onTitleChange={() => undefined}
        sessionId={sessionId}
        title="父对话"
      />,
    );
  }

  async function openModelModalAndSave() {
    fireEvent.click(screen.getByTitle('模型配置'));
    fireEvent.change(await screen.findByPlaceholderText('留空以保留当前值'), { target: { value: 'sk-test' } });
    fireEvent.click(await screen.findByRole('button', { name: '保存' }));
  }

  it('覆盖写到顶栏当前那个会话上', async () => {
    renderHeader('sess-abcdef');
    await openModelModalAndSave();

    await waitFor(() => {
      const targets = fetchMock.mock.calls.map(([input]) => String(input));
      expect(targets.some(url => url.includes('/v1/sessions/sess-abcdef/provider_override'))).toBe(true);
    });
  });

  it('没有活动会话时拒绝保存并说明原因', async () => {
    renderHeader('');
    await openModelModalAndSave();

    expect(await screen.findByText('没有活动会话')).toBeInTheDocument();
    const targets = fetchMock.mock.calls.map(([input]) => String(input));
    expect(targets.some(url => url.includes('/provider_override'))).toBe(false);
  });
});
