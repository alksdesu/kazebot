import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Blob as NodeBlob } from 'node:buffer';
import { createHash, webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ExtensionRegistry, extensions } from '../plugins/registry';
import { FrontendPluginRuntime, type FrontendCatalog, type FrontendPlugin } from '../plugins/runtime';
import { browserPluginState, updateBrowserPlugin, updateBrowserPlugins } from '../plugins/status';
import { PluginRender } from '../plugins/PluginRender';
import { type FrontendPluginSDK } from '../plugins/sdk';
import { fetchWithDeadline } from '../api/requestDeadline';
import { getPlugins, updatePluginConfig, checkAdminAuth } from '../api/supervisorClient';
import { WorkspaceNav } from '../features/WorkspaceNav';
import { useViewStore } from '../store/viewStore';
import { SettingsHeader } from '../components/settings/SettingsHeader';
import { SettingsPageHost } from '../components/settings/SettingsPageHost';
import { type SettingsTabDefinition } from '../components/settings/settingsTabs';
import App from '../App';
import { useChatStore } from '../store/chatStore';
import { useSettingsStore } from '../store/settingsStore';

const hash = (value: string) => createHash('sha256').update(value).digest('hex');
const response = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function plugin(source = 'first'): FrontendPlugin {
  return {
    id: 'test-plugin', desired_hash: hash(source), package_hash: hash(`package:${source}`),
    entry: { path: 'frontend.js', hash: hash(source) }, styles: [], config: {}, metadata: { priority: 100 },
  };
}
let runtime: FrontendPluginRuntime | undefined;
beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('Blob', NodeBlob);
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  updateBrowserPlugins({ settled: true, plugins: [], generation: '', error: '' });
  useViewStore.setState({ viewMode: 'chat', activeSettingsTab: 'general' });
});
afterEach(() => {
  cleanup(); runtime?.stop(); runtime = undefined;
  for (const id of ['test-plugin', 'first', 'second', 'broken', 'workspace-test']) extensions.remove(id);
  vi.unstubAllGlobals(); vi.restoreAllMocks();
  updateBrowserPlugins({ settled: true, plugins: [], generation: '', error: '' });
});

describe('owned registration transactions', () => {
  it('requires explicit collisions, layers by priority, and restores remaining generations', () => {
    const registry = new ExtensionRegistry();
    registry.builtin('commands', 'send', (value: string) => `base:${value}`);
    const first = registry.transaction('first', '1', 100);
    first.wrap<(value: string) => string>('commands', 'send', (next) => (value) => `first:${next(value)}`);
    first.commit();
    const invalid = registry.transaction('invalid', '1');
    invalid.register('commands', 'send', () => 'wrong');
    expect(() => invalid.commit()).toThrow('replace or wrap');
    const second = registry.transaction('second', '1', 200);
    second.wrap<(value: string) => string>('commands', 'send', (next) => (value) => `second:${next(value)}`);
    second.commit();
    expect(registry.resolve<(value: string) => string>('commands', 'send')!('x')).toBe('second:first:base:x');
    first.dispose();
    expect(registry.resolve<(value: string) => string>('commands', 'send')!('x')).toBe('second:base:x');
    const next = registry.transaction('second', '2');
    next.register('commands', 'send', () => 'new', { replace: true }); next.commit();
    second.dispose();
    expect(registry.resolve<() => string>('commands', 'send')!()).toBe('new');
    next.dispose();
    expect(registry.resolve<(value: string) => string>('commands', 'send')!('x')).toBe('base:x');
  });

  it('keeps committed registrations when an upgrade wrapper throws', () => {
    const registry = new ExtensionRegistry();
    registry.builtin('slots', 'header', 'base');
    const first = registry.transaction('test', '1'); first.register('slots', 'header', 'old', { replace: true }); first.commit();
    const next = registry.transaction('test', '2'); next.wrap('slots', 'header', () => { throw new Error('bad wrapper'); });
    expect(() => next.commit()).toThrow('bad wrapper'); next.dispose();
    expect(registry.resolve('slots', 'header')).toBe('old');
  });
});

describe('browser module lifecycle', () => {
  it('loads authenticated pinned assets and atomically upgrades styles and registrations', async () => {
    let source = 'first';
    let descriptor = plugin(source);
    let catalog: FrontendCatalog = { generation: '1', plugins: [descriptor] };
    const dispose = vi.fn();
    const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
      expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer token-a');
      if (input.endsWith('/frontend')) return response(catalog);
      expect(input).toContain(`/${descriptor.package_hash}/frontend.js`);
      return new Response(source);
    });
    vi.stubGlobal('fetch', fetcher);
    runtime = new FrontendPluginRuntime(async (text) => ({ activate: (sdk) => {
      sdk.slots.register('test.slot', () => text);
      sdk.styles.add(`.test-plugin { color: var(--duties-text); }`);
      return dispose;
    } }));
    runtime.start('token-a'); await runtime.refresh();
    expect(extensions.resolve<() => string>('slots', 'test.slot')!()).toBe('first');
    expect(document.querySelectorAll('style[data-plugin-owner="test-plugin"]')).toHaveLength(1);
    source = 'second'; descriptor = plugin(source); catalog = { generation: '2', plugins: [descriptor] };
    await runtime.refresh();
    expect(extensions.resolve<() => string>('slots', 'test.slot')!()).toBe('second');
    expect(dispose).toHaveBeenCalledOnce();
    expect(document.querySelectorAll('style[data-plugin-owner="test-plugin"]')).toHaveLength(1);
    catalog = { generation: '3', plugins: [] }; await runtime.refresh();
    expect(extensions.resolve('slots', 'test.slot')).toBeUndefined();
    expect(document.querySelectorAll('style[data-plugin-owner="test-plugin"]')).toHaveLength(0);
  });

  it('rolls back failed activation and preserves the previous active version', async () => {
    let source = 'first';
    vi.stubGlobal('fetch', vi.fn(async (input: string) => input.endsWith('/frontend') ? response({ generation: source, plugins: [plugin(source)] }) : new Response(source)));
    runtime = new FrontendPluginRuntime(async (text) => ({ activate: (sdk) => {
      sdk.slots.register('test.slot', () => text);
      sdk.styles.add('.test { color: var(--duties-text); }');
      if (text === 'bad') throw new Error('activation rejected');
    } }));
    runtime.start('token-a'); await runtime.refresh();
    source = 'bad'; await runtime.refresh();
    expect(extensions.resolve<() => string>('slots', 'test.slot')!()).toBe('first');
    expect(browserPluginState().plugins[0]).toMatchObject({ status: 'error', activeHash: hash('first'), desiredHash: hash('bad') });
    expect(document.querySelectorAll('style[data-plugin-owner="test-plugin"]')).toHaveLength(1);
  });

  it('keeps style precedence ordered by priority after an earlier plugin is upgraded', async () => {
    let firstSource = 'first';
    vi.stubGlobal('fetch', vi.fn(async (input: string) => input.endsWith('/frontend') ? response({ generation: firstSource, plugins: [
      { ...plugin(firstSource), id: 'first', metadata: { priority: 200 } },
      { ...plugin('second'), id: 'second', metadata: { priority: 100 } },
    ] }) : new Response(input.includes('/first/assets/') ? firstSource : 'second')));
    runtime = new FrontendPluginRuntime(async () => ({ activate: (sdk) => { sdk.styles.add(`.plugin-style { color: var(--duties-text); }`); } }));
    runtime.start('token-a'); await runtime.refresh();
    const owners = () => [...document.querySelectorAll<HTMLStyleElement>('style[data-plugin-owner]')].map((style) => style.dataset.pluginOwner);
    expect(owners()).toEqual(['second', 'first']);
    firstSource = 'first-upgrade'; await runtime.refresh();
    expect(owners()).toEqual(['second', 'first']);
  });

  it('rejects fingerprint mismatches before running module code', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string) => input.endsWith('/frontend') ? response({ generation: '1', plugins: [plugin()] }) : new Response('tampered')));
    const loader = vi.fn(); runtime = new FrontendPluginRuntime(loader);
    runtime.start('token-a'); await runtime.refresh();
    expect(loader).not.toHaveBeenCalled();
    expect(browserPluginState().plugins[0].errors.join()).toContain('fingerprint mismatch');
  });

  it('retains the active browser version for invalid enabled packages and unloads on actual disable', async () => {
    let catalog: FrontendCatalog = { generation: '1', plugins: [plugin()] };
    vi.stubGlobal('fetch', vi.fn(async (input: string) => input.endsWith('/frontend') ? response(catalog) : new Response('first')));
    runtime = new FrontendPluginRuntime(async () => ({ activate: (sdk) => { sdk.slots.register('test.slot', () => 'last good'); } }));
    runtime.start('token-a'); await runtime.refresh();
    catalog = { generation: '2', plugins: [], errors: [{ id: 'test-plugin', error: 'Invalid plugin.json', enabled: true }] };
    await runtime.refresh();
    expect(extensions.resolve<() => string>('slots', 'test.slot')!()).toBe('last good');
    expect(browserPluginState().plugins[0]).toMatchObject({ status: 'error', activeHash: hash('first'), errors: ['Invalid plugin.json'] });
    catalog = { generation: '3', plugins: [] }; await runtime.refresh();
    expect(extensions.resolve('slots', 'test.slot')).toBeUndefined();
  });

  it('does not revive an old activation after token rotation and disposes its late result', async () => {
    const activation = deferred<void>(); const started = deferred<void>(); const dispose = vi.fn();
    vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
      if (input.endsWith('/frontend')) return response({ generation: '1', plugins: new Headers(init?.headers).get('Authorization') === 'Bearer token-a' ? [plugin()] : [] });
      return new Response('first');
    }));
    runtime = new FrontendPluginRuntime(async () => ({ activate: async (sdk) => {
      sdk.slots.register('test.slot', () => 'stale'); started.resolve(); await activation.promise; return dispose;
    } }));
    runtime.start('token-a'); const previous = runtime.refresh(); await started.promise;
    runtime.start('token-b'); await runtime.refresh(); activation.resolve(); await previous;
    await waitFor(() => expect(dispose).toHaveBeenCalledOnce());
    expect(extensions.resolve('slots', 'test.slot')).toBeUndefined();
    expect(browserPluginState().plugins).toEqual([]);
  });

  it('isolates disposer failures and exposes them while continuing cleanup', async () => {
    let enabled = true; const lastCleanup = vi.fn();
    vi.stubGlobal('fetch', vi.fn(async (input: string) => input.endsWith('/frontend') ? response({ generation: '1', plugins: enabled ? [plugin()] : [] }) : new Response('first')));
    runtime = new FrontendPluginRuntime(async () => ({ activate: (sdk) => {
      sdk.lifecycle.onDispose(lastCleanup);
      sdk.lifecycle.onDispose(async () => { throw new Error('cleanup failed'); });
      sdk.slots.register('test.slot', () => 'loaded');
    } }));
    runtime.start('token-a'); await runtime.refresh(); enabled = false; await runtime.refresh();
    expect(lastCleanup).toHaveBeenCalledOnce();
    expect(extensions.resolve('slots', 'test.slot')).toBeUndefined();
    expect(browserPluginState().plugins[0].errors.join()).toContain('cleanup failed');
  });

  it('intercepts the shared transport and SDK HTTP calls while bypassing its own catalog and assets', async () => {
    let sdk!: FrontendPluginSDK; const intercepted: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: string) => input.endsWith('/frontend') ? response({ generation: '1', plugins: [plugin()] }) : input.endsWith('frontend.js') ? new Response('first') : response({ ok: true })));
    runtime = new FrontendPluginRuntime(async () => ({ activate: (value) => {
      sdk = value;
      sdk.requests.wrap((next) => (input, init) => { intercepted.push(input); return next(input, init); });
    } }));
    runtime.start('token-a'); await runtime.refresh();
    await fetchWithDeadline('/v1/features/materials');
    await sdk.http.request('/plugins/test-plugin/status');
    await runtime.refresh();
    expect(intercepted).toEqual(['/v1/features/materials', '/v1/plugins/test-plugin/status']);
    runtime.stop();
    expect(() => sdk.http.request('/plugins/test-plugin/status')).toThrow('expired');
  });

  it('keeps management and authentication usable when a middleware invocation throws', async () => {
    updateBrowserPlugin('broken', { generation: '1', status: 'loaded' });
    vi.stubGlobal('fetch', vi.fn(async () => response({ ok: true, plugins: [] })));
    const broken = extensions.transaction('broken', '1');
    broken.wrap('transport', 'request', () => async () => { throw new Error('middleware failed'); }); broken.commit();
    await expect(fetchWithDeadline('/v1/features/materials')).rejects.toThrow('middleware failed');
    expect(browserPluginState().plugins.find((item) => item.id === 'broken')?.errors).toContain('middleware failed');
    await expect(getPlugins('token-a')).resolves.toMatchObject({ plugins: [] });
    await expect(updatePluginConfig('token-a', 'broken', { enabled: false, config: {}, selectors: { nodes: [], channels: [], providers: [], purposes: [] }, base_hash: 'old' })).resolves.toEqual({ ok: true, plugins: [] });
    await expect(checkAdminAuth('token-a')).resolves.toBeTruthy();
    await expect(fetchWithDeadline('/i/example/v1/admin/runtime/status')).resolves.toBeInstanceOf(Response);
  });

  it('attributes a transport error to its originating layer and leaves native errors unattributed', async () => {
    updateBrowserPlugin('first', { generation: '1', status: 'loaded' });
    updateBrowserPlugin('second', { generation: '1', status: 'loaded' });
    const first = extensions.transaction('first', '1', 100);
    first.wrap('transport', 'request', () => async () => { throw new Error('first failed'); }); first.commit();
    const second = extensions.transaction('second', '1', 200);
    second.wrap<(input: string, init: RequestInit) => Promise<Response>>('transport', 'request', (next) => (input, init) => next(input, init)); second.commit();
    await expect(fetchWithDeadline('/v1/features')).rejects.toThrow('first failed');
    expect(browserPluginState().plugins.find((item) => item.id === 'first')?.errors).toEqual(['first failed']);
    expect(browserPluginState().plugins.find((item) => item.id === 'second')?.errors).toEqual([]);
    first.dispose(); vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network offline')));
    await expect(fetchWithDeadline('/v1/features')).rejects.toThrow('network offline');
    expect(browserPluginState().plugins.find((item) => item.id === 'second')?.errors).toEqual([]);
  });

  it('stops timers, listeners and subscriptions before awaiting a user disposer', async () => {
    const pending = deferred<void>(); const entered = deferred<void>();
    const timer = vi.fn(); const listener = vi.fn(); const subscriber = vi.fn();
    vi.stubGlobal('fetch', vi.fn(async (input: string) => input.endsWith('/frontend') ? response({ generation: '1', plugins: [plugin()] }) : new Response('first')));
    runtime = new FrontendPluginRuntime(async () => ({ activate: (sdk) => {
      sdk.timers.interval(timer, 5);
      sdk.events.listen(window, 'plugin-test', listener);
      sdk.state.view.subscribe(subscriber);
      return async () => { entered.resolve(); await pending.promise; };
    } }));
    runtime.start('token-a'); await runtime.refresh();
    runtime.stop(); await entered.promise;
    const calls = timer.mock.calls.length;
    window.dispatchEvent(new Event('plugin-test'));
    useViewStore.setState({ activeSettingsTab: 'client' });
    await new Promise((resolve) => window.setTimeout(resolve, 30));
    expect(timer).toHaveBeenCalledTimes(calls);
    expect(listener).not.toHaveBeenCalled();
    expect(subscriber).not.toHaveBeenCalled();
    pending.resolve();
  });
});

describe('application extension hosts', () => {
  it('renders an added view and wrapped application layout through the actual App host', () => {
    const original = useChatStore.getState().loadStartup;
    const auth = useSettingsStore.getState();
    useChatStore.setState({ loadStartup: vi.fn().mockResolvedValue(undefined) });
    useSettingsStore.setState({ isAuthenticated: true, adminToken: '' });
    vi.stubGlobal('fetch', vi.fn(async () => response({})));
    const transaction = extensions.transaction('workspace-test', '1');
    transaction.register('views', 'test-workspace', {
      id: 'test-workspace', header: () => <h1>动态工作区标题</h1>, sidebar: () => <nav>插件导航</nav>, main: () => <p>动态工作区正文</p>, hasRightPanel: () => false,
    });
    transaction.wrap<(props: any) => React.ReactNode>('slots', 'app.layout', (next) => (props) => <><p>应用布局包装</p>{next(props)}</>);
    transaction.commit();
    useViewStore.setState({ viewMode: 'test-workspace' });
    const mounted = render(<App />);
    expect(screen.getByText('动态工作区正文')).toBeInTheDocument();
    expect(screen.getByText('应用布局包装')).toBeInTheDocument();
    mounted.unmount();
    useChatStore.setState({ loadStartup: original });
    useSettingsStore.setState({ isAuthenticated: auth.isAuthenticated, adminToken: auth.adminToken });
  });
  it('renders new workspaces in navigation and restores the built-in list on unload', () => {
    const transaction = extensions.transaction('workspace-test', '1');
    transaction.register('workspaces', 'new-workspace', { id: 'new-workspace', label: '测试工作区', order: 30 }); transaction.commit();
    render(<WorkspaceNav />);
    fireEvent.click(screen.getByRole('button', { name: '测试工作区' }));
    expect(useViewStore.getState().viewMode).toBe('new-workspace');
    act(() => transaction.dispose());
    expect(screen.queryByRole('button', { name: '测试工作区' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '聊天' })).toBeInTheDocument();
  });

  it('falls back to the next owner when a plugin component throws', () => {
    updateBrowserPlugin('broken', { generation: '1', status: 'loaded' });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const first = extensions.transaction('first', '1'); first.register('slots', 'test.slot', () => <p>working</p>); first.commit();
    const broken = extensions.transaction('broken', '1'); broken.register('slots', 'test.slot', () => { throw new Error('component failed'); }, { replace: true, priority: 200 }); broken.commit();
    render(<PluginRender<() => React.ReactNode> kind="slots" id="test.slot" render={(Component) => <Component />} />);
    expect(screen.getByText('working')).toBeInTheDocument();
    expect(browserPluginState().plugins.find((item) => item.id === 'broken')?.errors).toContain('component failed');
  });

  it('preserves a pending plugin settings deep link, then renders its registered page', () => {
    updateBrowserPlugins({ settled: false });
    useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'plugin-settings' });
    render(<><SettingsHeader /><SettingsPageHost /></>);
    expect(useViewStore.getState().activeSettingsTab).toBe('plugin-settings');
    const transaction = extensions.transaction('test-plugin', '1');
    act(() => {
      transaction.register<SettingsTabDefinition>('settings', 'plugin-settings', { id: 'plugin-settings', label: '插件设置', order: 3, Page: () => <p>真实插件页面</p> });
      transaction.commit(); updateBrowserPlugins({ settled: true, generation: '1' });
    });
    expect(screen.getByText('真实插件页面')).toBeInTheDocument();
    expect(useViewStore.getState().activeSettingsTab).toBe('plugin-settings');
  });
});
