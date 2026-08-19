// 会话级模型覆盖的候选。覆盖值是原样送进上游请求体的，节点文件那套 $ENV{} 展开
// 在这条路径上不生效，所以候选只能来自渠道配置。
import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SessionConfigPanel } from '../components/settings/SessionConfigPanel';
import { useChatStore } from '../store/chatStore';
import { useSettingsStore } from '../store/settingsStore';
import type { NodeDef } from '../types';

const activeNode: NodeDef = {
  id: 'ereuna_main',
  type: 'ai',
  name: 'EreunaMain',
  model: '$ENV{QQ_MAIN_MODEL}',
};

const block = (model: string, raw = model) =>
  ({ base_url: '', base_url_raw: '', model, model_raw: raw, api_key_present: false, api_key_redacted: '' });

const PROVIDERS = {
  active_provider: 'openai',
  providers: {
    openai: block('gpt-4o-mini'),
    deepseek: block('deepseek-v4-pro'),
  },
  // 展开不出来的引用后端会给空 model，但真送到前端的形状不一定干净 —— 混两条
  // 未展开的进来，确认它们进不了候选。
  fallbacks: [
    { ...block('claude-sonnet-4-6'), provider: 'openai' },
    { ...block('$ENV{BACKUP_MODEL}'), provider: 'backup' },
    { ...block('${SHELL_MODEL}'), provider: 'shell' },
  ],
  registered: ['openai', 'deepseek'],
};

function responseJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function seedStores() {
  localStorage.clear();
  useSettingsStore.setState({
    adminToken: 'test-token',
    isAuthenticated: true,
    availableNodes: [activeNode],
    activeNodeId: 'ereuna_main',
    activeNodeIsOverride: false,
    defaultNodeId: 'ereuna_main',
    entryNodeId: 'ereuna_main',
    globalModel: 'global-model',
    globalBaseUrl: 'https://global.example/v1',
    sessionProviderOverride: null,
    modelConfig: null,
  });
  useChatStore.setState({ connectionStatus: 'open' });
}

function stubSupervisorFetch(providersStatus = 200) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/config/providers')) {
      return providersStatus === 200 ? responseJson(PROVIDERS) : responseJson({ detail: '403' }, providersStatus);
    }
    if (url.endsWith('/active_node')) {
      return responseJson({ node_id: 'ereuna_main', is_override: false, default_node_id: 'ereuna_main' });
    }
    if (url.endsWith('/provider_override')) {
      return responseJson({});
    }
    if (url.endsWith('/config/openai/secret')) {
      return responseJson({ model: 'global-model', base_url: 'https://global.example/v1', api_key_present: false });
    }
    if (url.endsWith('/config')) {
      return responseJson({ provider: 'openai', openai: { model: 'global-model', base_url: 'https://global.example/v1' } });
    }
    return responseJson({});
  }));
}

// 继承值是 $ENV{...} 时 placeholder 摆的是变量名，不是那个照抄进去必挂的模板。
const INHERITED_PLACEHOLDER = '继承自环境变量 QQ_MAIN_MODEL';

const modelBox = () => screen.getByPlaceholderText(INHERITED_PLACEHOLDER) as HTMLInputElement;

// 面板里不止一张候选表，按输入框的 list 指向取，别把供应商那张也扫进来。
const optionsOf = (container: HTMLElement, input: HTMLInputElement) => {
  const id = input.getAttribute('list');
  if (!id) return [];
  return [...container.querySelectorAll(`#${CSS.escape(id)} option`)].map((node) => (node as HTMLOptionElement).value);
};

const optionValues = (container: HTMLElement) => optionsOf(container, modelBox());

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  useChatStore.getState().resetState();
});

describe('会话模型候选', () => {
  it('摆的是渠道和备用链里配好的模型', async () => {
    seedStores();
    stubSupervisorFetch();

    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    expect(optionValues(container)).toEqual(['gpt-4o-mini', 'deepseek-v4-pro', 'claude-sonnet-4-6']);
    // 光渲染出候选表不够，输入框得真的指着一张 datalist。
    const listId = modelBox().getAttribute('list');
    expect(listId).toBeTruthy();
    expect(container.querySelector(`#${CSS.escape(listId!)}`)?.tagName).toBe('DATALIST');
  });

  it('备用链里未展开的模板同样不进候选', async () => {
    // 节点那条路堵住了不代表这条也堵住了：备用链是另一个来源，后端不给它做展开。
    seedStores();
    stubSupervisorFetch();

    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    expect(optionValues(container)).not.toContain('$ENV{BACKUP_MODEL}');
    expect(optionValues(container)).not.toContain('${SHELL_MODEL}');
    expect(optionValues(container)).toContain('claude-sonnet-4-6');
  });

  it('不把节点写的 $ENV{...} 摆进候选', async () => {
    seedStores();
    stubSupervisorFetch();

    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    expect(optionValues(container)).not.toContain('$ENV{QQ_MAIN_MODEL}');
    // 继承值仍然由 placeholder 交代，不靠候选表重复一遍。
    expect(modelBox()).toHaveAttribute('placeholder', INHERITED_PLACEHOLDER);
  });

  it('placeholder 不把 $ENV{...} 原样摆出来当可填的值', async () => {
    // 会话覆盖是原样送上游的，照着 placeholder 抄一遍换来的是 400。
    seedStores();
    stubSupervisorFetch();

    render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(screen.getByText('会话模型')).toBeInTheDocument());

    expect(screen.queryByPlaceholderText('$ENV{QQ_MAIN_MODEL}')).toBeNull();
    expect(modelBox()).toBeInTheDocument();
  });

  it('生效值那行也摆变量名，不摆模板', async () => {
    seedStores();
    stubSupervisorFetch();

    render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(screen.getByText('会话模型')).toBeInTheDocument());

    expect(screen.getByText(/环境变量 QQ_MAIN_MODEL/)).toBeInTheDocument();
  });

  it('供应商框摆的是后端注册过的渠道名', async () => {
    seedStores();
    stubSupervisorFetch();

    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(container.querySelectorAll('datalist')).toHaveLength(2));

    const providerBox = screen.getByLabelText('供应商') as HTMLInputElement;
    expect(optionsOf(container, providerBox)).toEqual(['openai', 'deepseek']);
  });

  it('令牌被清掉后候选跟着清空', async () => {
    // 上一位管理员的渠道模型不该在未认证状态下继续挂在 datalist 里。
    seedStores();
    stubSupervisorFetch();

    const { container } = render(<SessionConfigPanel sessionId="session-1" />);
    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    act(() => { useSettingsStore.setState({ adminToken: null, isAuthenticated: false }); });

    await waitFor(() => expect(container.querySelector('datalist')).toBeNull());
    expect(modelBox()).not.toHaveAttribute('list');
    expect(screen.getByLabelText('供应商')).not.toHaveAttribute('list');
  });

  it('渠道配置拉不到就退成纯输入框，面板其余部分照常', async () => {
    seedStores();
    stubSupervisorFetch(403);

    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(screen.getByText('会话模型')).toBeInTheDocument());

    expect(container.querySelector('datalist')).toBeNull();
    expect(modelBox()).not.toHaveAttribute('list');
    expect(screen.getAllByText('（节点：ereuna_main）').length).toBeGreaterThan(0);
  });
});
