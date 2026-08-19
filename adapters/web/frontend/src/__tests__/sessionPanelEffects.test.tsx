// 会话面板的数据拉取：哪些跟会话走、哪些不跟，以及慢响应回来时归谁。
import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SessionConfigPanel } from '../components/settings/SessionConfigPanel';
import { useChatStore } from '../store/chatStore';
import { useSettingsStore } from '../store/settingsStore';

const NODES = [{ id: 'ereuna_main', type: 'ai', name: 'EreunaMain', model: 'node-model' }];

const OVERRIDES: Record<string, Record<string, unknown>> = {
  'session-1': { model: 'first-session-model' },
  'session-2': { model: 'second-session-model' },
};

function responseJson(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function defaultBody(url: string): unknown {
  if (url.endsWith('/admin/config/nodes')) return NODES;
  if (url.endsWith('/config/providers')) {
    return {
      active_provider: 'openai',
      // 全局渠道配了 key —— 面板不该拿它冒充某个会话的专属密钥。
      providers: {
        openai: {
          model: 'global-model', model_raw: 'global-model',
          base_url: 'https://global.example/v1', base_url_raw: 'https://global.example/v1',
          api_key_present: true, api_key_redacted: '****cdef',
        },
      },
      fallbacks: [],
      registered: ['openai'],
    };
  }
  if (url.endsWith('/active_node')) {
    return { node_id: 'ereuna_main', is_override: false, default_node_id: 'ereuna_main' };
  }
  if (url.endsWith('/provider_override')) {
    const sid = /sessions\/([^/]+)\/provider_override/.exec(url)?.[1] || '';
    return OVERRIDES[sid] ?? {};
  }
  if (url.endsWith('/config')) {
    return { provider: 'openai', openai: { model: 'global-model', base_url: 'https://global.example/v1' } };
  }
  return {};
}

type Held = { url: string; release: () => void };

function installFetch(shouldHold: (url: string) => boolean = () => false) {
  const calls: string[] = [];
  const held: Held[] = [];
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (!shouldHold(url)) return Promise.resolve(responseJson(defaultBody(url)));
    return new Promise<Response>((resolve) => {
      held.push({ url, release: () => resolve(responseJson(defaultBody(url))) });
    });
  }));
  return { calls, held };
}

const countOf = (calls: string[], fragment: string) => calls.filter((url) => url.includes(fragment)).length;

beforeEach(() => {
  localStorage.clear();
  useSettingsStore.setState({
    adminToken: 'test-token',
    isAuthenticated: true,
    availableNodes: [],
    activeNodeId: '',
    activeNodeIsOverride: false,
    defaultNodeId: '',
    entryNodeId: '',
    globalModel: '',
    globalBaseUrl: '',
    sessionProviderOverride: null,
    modelConfig: null,
  });
  useChatStore.setState({ connectionStatus: 'open' });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  useChatStore.getState().resetState();
});

describe('会话面板的拉取边界', () => {
  it('节点清单只拉一次，不被自己写回 store 的动作带着重跑', async () => {
    const { calls } = installFetch();

    render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(useSettingsStore.getState().availableNodes).toHaveLength(1));
    // 拿到清单后 store 变了，如果它还在依赖数组里，这里就会是 2。
    await waitFor(() => expect(countOf(calls, '/admin/config/nodes')).toBe(1));
  });

  it('store 里已经有节点清单就不再去拉', async () => {
    // 从别的设置页进来时清单已经填好了，面板没有理由再要一遍。
    useSettingsStore.setState({ availableNodes: NODES as never });
    const { calls } = installFetch();

    render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(countOf(calls, '/provider_override')).toBe(1));
    expect(countOf(calls, '/admin/config/nodes')).toBe(0);
  });

  it('换会话不重拉与会话无关的东西', async () => {
    const { calls } = installFetch();

    const { rerender } = render(<SessionConfigPanel sessionId="session-1" />);
    await waitFor(() => expect(countOf(calls, '/provider_override')).toBe(1));

    rerender(<SessionConfigPanel sessionId="session-2" />);
    await waitFor(() => expect(countOf(calls, '/provider_override')).toBe(2));

    expect(countOf(calls, '/admin/config/nodes')).toBe(1);
    expect(countOf(calls, '/config/providers')).toBe(1);
  });

  it('界面不碰返回明文密钥的那个端点', async () => {
    // /config/openai/secret 是 engine 取密钥的通道，浏览器要的三个字段
    // /config/providers 全有，而且那份是脱敏的。
    const { calls } = installFetch();

    render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(useSettingsStore.getState().modelConfig).not.toBeNull());
    expect(countOf(calls, '/secret')).toBe(0);
    expect(useSettingsStore.getState().modelConfig).toEqual({
      model: 'global-model', base_url: 'https://global.example/v1', api_key_present: true,
    });
  });

  it('节点清单晚到不会把已经拿到的会话覆盖标签打回继承', async () => {
    // 原来的写法里节点清单一回来就重跑整个 effect，标签先被清空再拉一遍。
    const { calls, held } = installFetch((url) => url.includes('/admin/config/nodes'));

    render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(screen.getByText('会话')).toBeInTheDocument());
    const overrideCalls = countOf(calls, '/provider_override');

    await act(async () => { held.forEach((h) => h.release()); });

    expect(screen.getByText('会话')).toBeInTheDocument();
    expect(countOf(calls, '/provider_override')).toBe(overrideCalls);
  });

  it('上一个会话的慢响应回来时不覆盖当前会话', async () => {
    const { held } = installFetch((url) => url.includes('/provider_override'));

    const { rerender } = render(<SessionConfigPanel sessionId="session-1" />);
    await waitFor(() => expect(held).toHaveLength(1));

    rerender(<SessionConfigPanel sessionId="session-2" />);
    await waitFor(() => expect(held).toHaveLength(2));

    // 后发的先回，先发的后回 —— 面板要显示的是当前这个会话的值。
    await act(async () => { held[1].release(); });
    await act(async () => { held[0].release(); });

    await waitFor(() => expect(screen.getByDisplayValue('second-session-model')).toBeInTheDocument());
    expect(screen.queryByDisplayValue('first-session-model')).toBeNull();
  });

  it('还没有会话时，全局那把钥匙不算这个会话的', async () => {
    // /config/openai/secret 报 api_key_present: true。没有 sid 就没人去覆盖这个值，
    // 全局的 true 会一直挂在标签上，让人以为当前会话配了专属密钥。
    installFetch();

    render(<SessionConfigPanel sessionId="no-session" />);

    await waitFor(() => expect(useSettingsStore.getState().modelConfig).not.toBeNull());

    expect(screen.getByText(/API 密钥 （可选）/)).toBeInTheDocument();
  });

  it('会话覆盖拉不到时同样不借用全局那把', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/provider_override')) return Promise.resolve(new Response('nope', { status: 403 }));
      return Promise.resolve(responseJson(defaultBody(url)));
    }));

    render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(useSettingsStore.getState().modelConfig).not.toBeNull());

    expect(screen.getByText(/API 密钥 （可选）/)).toBeInTheDocument();
  });

  it('会话自己配了密钥时才说已设置', async () => {
    OVERRIDES['with-key'] = { model: 'm', api_key: 'sk-session' };
    installFetch();

    render(<SessionConfigPanel sessionId="with-key" />);

    await waitFor(() => expect(screen.getByText(/API 密钥 （已设置）/)).toBeInTheDocument());
    delete OVERRIDES['with-key'];
  });

});
