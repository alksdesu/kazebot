// 控制台入口与三个上下文右栏的注册回归。
// 控制台此前只能靠 ?view=console 深链进入，三个右栏组件实现完整却从未被注册。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Sidebar } from '../components/layout';
import { SettingsRightPanel } from '../components/settings/SettingsRightPanel';
import { SettingsSidebar } from '../components/settings/SettingsSidebar';
import { SystemSettingsPage } from '../components/settings/pages/SystemSettingsPage';
import { useConsoleStore } from '../console/consoleStore';
import { useChatStore, type ConversationMeta } from '../store/chatStore';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
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

describe('控制台入口', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/');
    useViewStore.setState({ viewMode: 'chat', activeSettingsTab: 'general' });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('设置侧栏展开后能直接进到控制台的指定分区', () => {
    render(<SettingsSidebar />);

    fireEvent.click(screen.getByRole('button', { name: 'QQ 控制台' }));
    fireEvent.click(screen.getByRole('button', { name: 'QQ 控制台 权限' }));

    expect(useViewStore.getState().viewMode).toBe('console');
    expect(useConsoleStore.getState().domain).toBe('permissions');
  });

  it('聊天侧栏只留设置入口，控制台入口收敛到设置里', () => {
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

  it('进入控制台会写进地址栏', () => {
    useViewStore.getState().openConsole();

    expect(new URLSearchParams(window.location.search).get('view')).toBe('console');
  });

  it('退出后地址栏不再残留 view，刷新不会弹回控制台', () => {
    window.history.replaceState(null, '', '/?view=console');

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

  it('控制台不携带设置分区参数', () => {
    useViewStore.getState().openSettings('tools');
    useViewStore.getState().openConsole();

    const params = new URLSearchParams(window.location.search);
    expect(params.get('view')).toBe('console');
    expect(params.get('tab')).toBeNull();
  });

  it('不动其他人的 query 参数', () => {
    window.history.replaceState(null, '', '/?token=abc&domain=timing');

    useViewStore.getState().openConsole();

    const params = new URLSearchParams(window.location.search);
    expect(params.get('token')).toBe('abc');
    expect(params.get('domain')).toBe('timing');
  });

  it('控制台切换域会写进地址栏', () => {
    useViewStore.getState().openConsole();

    useConsoleStore.getState().setDomain('permissions');

    expect(new URLSearchParams(window.location.search).get('domain')).toBe('permissions');
  });

  it('离开控制台后不残留域参数', () => {
    useViewStore.getState().openConsole();
    useConsoleStore.getState().setDomain('permissions');

    useViewStore.getState().closeSettings();

    expect(new URLSearchParams(window.location.search).get('domain')).toBeNull();
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

describe('模型页右栏的会话来源', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
    useViewStore.setState({ activeSettingsTab: 'model' });
    fetchMock = vi.fn(async () => jsonResponse({}));
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function editAndSave() {
    fireEvent.change(screen.getByPlaceholderText('留空以保留当前值'), { target: { value: 'sk-test' } });
    fireEvent.click(await screen.findByRole('button', { name: '保存' }));
  }

  it('覆盖写到聊天里当前选中的会话上', async () => {
    useChatStore.setState({ conversations: [conversation], activeConversationId: 'conv-1' });

    render(<SettingsRightPanel />);
    await editAndSave();

    await waitFor(() => {
      const targets = fetchMock.mock.calls.map(([input]) => String(input));
      expect(targets.some(url => url.includes('/v1/sessions/sess-abcdef/provider_override'))).toBe(true);
    });
  });

  it('没有活动会话时拒绝保存并说明原因', async () => {
    useChatStore.setState({ conversations: [], activeConversationId: null });

    render(<SettingsRightPanel />);
    await editAndSave();

    expect(await screen.findByText('没有活动会话')).toBeInTheDocument();
    const targets = fetchMock.mock.calls.map(([input]) => String(input));
    expect(targets.some(url => url.includes('/provider_override'))).toBe(false);
  });
});
