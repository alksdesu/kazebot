import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminPlugin, PluginsResponse } from '../api/supervisorClient';
import { installPluginPackage } from '../api/supervisorClient';
import { PluginsSettingsPage } from '../components/settings/pages/PluginsSettingsPage';
import { currentSettingsTabs } from '../components/settings/settingsTabs';
import { useSettingsStore } from '../store/settingsStore';

function plugin(overrides: Partial<AdminPlugin> = {}): AdminPlugin {
  return {
    id: 'request_filter', name: '请求过滤', version: '1.2', description: '请求前处理', author: '测试作者',
    source: 'custom', filename: 'request_filter.py', source_hash: 'source-1', desired_hash: 'desired-1', enabled: false,
    config: { prefix: 'hello', count: 3, active: true, mode: 'safe', custom: { keep: ['a'] } },
    selectors: { nodes: [], channels: [], providers: [], purposes: [] },
    params_schema: [
      { key: 'prefix', type: 'string', label: '前缀' },
      { key: 'count', type: 'integer', label: '次数', minimum: 1, maximum: 10 },
      { key: 'active', type: 'boolean', label: '内部开关' },
      { key: 'mode', type: 'enum', label: '模式', choices: [{ value: 'safe', label: '安全' }, { value: 'fast', label: '快速' }] },
      { key: 'custom', type: 'object', label: '扩展对象' },
    ],
    metadata: { license: 'MIT' }, error: null, ...overrides,
  };
}

function catalog(plugins = [plugin()]): PluginsResponse {
  return {
    plugins, generation: 'generation-1', shared_group: 'shared-demo',
    runtime: [{ pid: 123, process: 'engine', alive: true, generation: 'older', updated_at: '2026-10-18', plugins: [{ id: 'request_filter', desired_hash: 'older', status: 'loaded' }] }],
  };
}

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function stubApi(data = catalog(), intercept?: (url: string, init?: RequestInit) => Promise<Response> | Response | undefined) {
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const custom = intercept?.(url, init);
    if (custom) return custom;
    if (url === '/v1/admin/plugins' && !init?.method) return response(data);
    if (url.endsWith('/source')) return response({ content: 'PLUGIN_META = {}\n', hash: 'source-1' });
    if (url.endsWith('/nodes')) return response([{ id: 'main', name: '主节点' }]);
    if (url.endsWith('/config/providers')) return response({ providers: { my_channel: { type: 'openai' } }, registered: ['openai', 'anthropic'] });
    return response({});
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

async function openPage() {
  render(<PluginsSettingsPage />);
  await screen.findByLabelText('插件 Python 源码');
}

beforeEach(() => {
  useSettingsStore.setState({ adminToken: 'token-a', isAuthenticated: true });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('系统插件包管理', () => {
  const packagePlugin = () => plugin({ kind: 'package', filename: 'system-workspace', source_editable: false, files: [{ path: 'frontend.js', size: 10, hash: 'script-hash' }], frontend: { entry: { path: 'frontend.js', hash: 'script-hash' }, styles: [] } });
  function zipFile() {
    const file = new File(['ZIP'], 'system-workspace.zip', { type: 'application/zip' });
    Object.defineProperty(file, 'arrayBuffer', { value: () => Promise.resolve(new Uint8Array([80, 75, 3, 4]).buffer) });
    return file;
  }

  it('以文件清单和浏览器状态展示插件包，不打开单文件源码编辑器', async () => {
    const fetcher = stubApi(catalog([packagePlugin()]));
    render(<PluginsSettingsPage />);
    await screen.findByRole('heading', { name: '插件包文件' });
    expect(screen.getByText('frontend.js · 10 bytes')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '浏览器实际状态' })).toBeInTheDocument();
    expect(screen.queryByLabelText('插件 Python 源码')).not.toBeInTheDocument();
    expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/source'))).toBe(false);
  });

  it('通过带当前版本指纹的 ZIP 替换接口更新插件包', async () => {
    const fetcher = stubApi(catalog([packagePlugin()]), (url, init) => url.endsWith('/packages') && init?.method === 'POST' ? response({ ...packagePlugin(), desired_hash: 'package-v2' }) : undefined);
    render(<PluginsSettingsPage />);
    fireEvent.change(await screen.findByLabelText('替换插件包'), { target: { files: [zipFile()] } });
    await screen.findByText('插件包已替换，浏览器和运行进程会分别应用新版本。');
    const request = fetcher.mock.calls.find(([url]) => String(url).endsWith('/packages'));
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({ filename: 'system-workspace.zip', content_base64: 'UEsDBA==', base_hash: 'desired-1' });
  });

  it('文件读取期间令牌变化时停止上传', async () => {
    const read = deferred<ArrayBuffer>(); const file = new File(['ZIP'], 'plugin.zip');
    Object.defineProperty(file, 'arrayBuffer', { value: () => read.promise });
    const fetcher = stubApi(); let current = true;
    const attempt = installPluginPackage('token-a', file, undefined, () => current);
    current = false; read.resolve(new Uint8Array([80, 75]).buffer);
    await expect(attempt).rejects.toThrow('登录状态已变化');
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe('插件设置导航与编辑', () => {
  it('插件独立放在通用和客户端下面', () => {
    expect(currentSettingsTabs().filter((tab) => tab.group === 'basics').map((tab) => tab.id)).toEqual(['general', 'client', 'plugins']);
    expect(currentSettingsTabs().find((tab) => tab.id === 'plugins')).toMatchObject({ label: '插件', order: 2, Page: PluginsSettingsPage });
  });

  it('未认证时不读取插件或源码', () => {
    useSettingsStore.setState({ isAuthenticated: false });
    const fetcher = stubApi();
    render(<PluginsSettingsPage />);
    expect(screen.getByText('请先在通用页面登录 Admin Token')).toBeInTheDocument();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('保存表单、作用范围与未声明 JSON 参数，并发送原始版本指纹', async () => {
    const fetcher = stubApi(catalog(), (url, init) => url.endsWith('/config') ? response(plugin({ ...JSON.parse(String(init?.body)), desired_hash: 'desired-2' })) : undefined);
    await openPage();
    expect(screen.getByText(/共享组「shared-demo」/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('启用插件'));
    fireEvent.change(screen.getByLabelText('前缀'), { target: { value: 'changed' } });
    fireEvent.change(screen.getByLabelText('模式'), { target: { value: '1' } });
    await waitFor(() => expect(within(screen.getByLabelText('节点')).getByText('main')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('节点'), { target: { value: 'main' } });
    fireEvent.change(screen.getByLabelText('模型渠道'), { target: { value: 'my_channel' } });
    fireEvent.change(screen.getByLabelText('Provider'), { target: { value: 'openai' } });
    fireEvent.change(screen.getByLabelText('请求用途'), { target: { value: 'inference' } });
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(fetcher.mock.calls.some(([input, init]) => String(input).endsWith('/config') && init?.method === 'PUT')).toBe(true));
    const call = fetcher.mock.calls.find(([input]) => String(input).endsWith('/config'))!;
    expect(JSON.parse(String(call[1]?.body))).toEqual({
      enabled: true, base_hash: 'desired-1',
      config: { prefix: 'changed', count: 3, active: true, mode: 'fast', custom: { keep: ['a'] } },
      selectors: { nodes: ['main'], channels: ['my_channel'], providers: ['openai'], purposes: ['inference'] },
    });
    await screen.findByText(/配置已保存，运行进程会自动更新/);
    expect(screen.getByRole('button', { name: '保存配置' })).toBeDisabled();
  });

  it('非法 JSON 和超出 schema 范围的数字不能保存', async () => {
    stubApi();
    await openPage();
    fireEvent.click(screen.getByLabelText('启用插件'));
    fireEvent.change(screen.getByLabelText('次数'), { target: { value: '0' } });
    expect(screen.getByText('次数不能小于 1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存配置' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('插件参数 JSON'), { target: { value: '[]' } });
    expect(screen.getByText('参数 JSON 必须是对象')).toBeInTheDocument();
    expect(screen.getByLabelText('前缀')).toBeDisabled();
  });

  it('参数失效的插件仍可停用，不要求先修好 schema 参数', async () => {
    const broken = plugin({ enabled: true, config: { count: 'invalid' } });
    const fetcher = stubApi(catalog([broken]), (url) => url.endsWith('/config') ? response({ ...broken, enabled: false }) : undefined);
    await openPage();
    expect(screen.getByText('次数必须是数字')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('启用插件'));
    expect(screen.getByRole('button', { name: '保存配置' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await screen.findByText(/配置已保存，运行进程会自动更新/);
    const call = fetcher.mock.calls.find(([input]) => String(input).endsWith('/config'))!;
    expect(JSON.parse(String(call[1]?.body))).toMatchObject({ enabled: false, config: { count: 'invalid' } });
  });

  it('409 保留草稿，并且刷新与选择其他插件先确认未保存修改', async () => {
    const fetcher = stubApi(catalog([plugin(), plugin({ id: 'second', name: '第二插件' })]), (url) => url.endsWith('/config') ? response({ detail: 'changed' }, 409) : undefined);
    await openPage();
    fireEvent.change(screen.getByLabelText('前缀'), { target: { value: 'draft' } });
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await screen.findByText(/插件已被其他页面修改/);
    expect(screen.getByLabelText('前缀')).toHaveValue('draft');
    vi.mocked(window.confirm).mockReturnValue(false);
    const count = fetcher.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: '刷新列表' }));
    fireEvent.click(screen.getByRole('button', { name: /第二插件/ }));
    expect(screen.getByLabelText('前缀')).toHaveValue('draft');
    expect(fetcher).toHaveBeenCalledTimes(count);
  });

  it('运行状态刷新不替换未保存草稿或旧配置指纹', async () => {
    let reads = 0;
    const fetcher = stubApi(catalog(), (url, init) => {
      if (url === '/v1/admin/plugins' && !init?.method) {
        reads += 1;
        return response(catalog([plugin({ desired_hash: reads > 1 ? 'external-change' : 'desired-1', config: { prefix: 'external' } })]));
      }
      if (url.endsWith('/config')) return response(plugin());
      return undefined;
    });
    await openPage();
    fireEvent.change(screen.getByLabelText('前缀'), { target: { value: 'draft' } });
    fireEvent.click(screen.getByRole('button', { name: '刷新进程状态' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新进程状态' })).toBeEnabled());
    expect(screen.getByLabelText('前缀')).toHaveValue('draft');
    expect(screen.getAllByText('配置已在别处更新')).toHaveLength(2);
    expect(screen.getByText(/当前草稿保持不变，请刷新列表/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(fetcher.mock.calls.some(([input]) => String(input).endsWith('/config'))).toBe(true));
    expect(JSON.parse(String(fetcher.mock.calls.find(([input]) => String(input).endsWith('/config'))![1]?.body)).base_hash).toBe('desired-1');
  });

  it('内置插件源码只读且无法删除，但仍可修改配置', async () => {
    stubApi(catalog([plugin({ source: 'bundled' })]));
    await openPage();
    expect(screen.getByLabelText('插件 Python 源码')).toHaveAttribute('readonly');
    expect(screen.queryByRole('button', { name: '删除插件' })).toBeNull();
    expect(screen.queryByLabelText('替换插件文件')).toBeNull();
    fireEvent.click(screen.getByLabelText('启用插件'));
    expect(screen.getByRole('button', { name: '保存配置' })).toBeEnabled();
  });

  it('源码保存使用读取的 hash，配置保存可继续使用本次源码修改后的 hash', async () => {
    const fetcher = stubApi(catalog(), (url, init) => {
      if (url.endsWith('/source') && init?.method === 'PUT') return response(plugin({ source_hash: 'source-2', desired_hash: 'desired-2' }));
      if (url.endsWith('/config')) return response(plugin({ desired_hash: 'desired-3' }));
      return undefined;
    });
    await openPage();
    fireEvent.change(screen.getByLabelText('插件 Python 源码'), { target: { value: 'PLUGIN_META = {"name": "changed"}' } });
    fireEvent.change(screen.getByLabelText('前缀'), { target: { value: 'draft' } });
    fireEvent.click(screen.getByRole('button', { name: '保存源码' }));
    await screen.findByText(/源码已保存，运行进程会自动更新/);
    expect(screen.getByLabelText('前缀')).toHaveValue('draft');
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(fetcher.mock.calls.some(([input]) => String(input).endsWith('/config'))).toBe(true));
    const source = fetcher.mock.calls.find(([input, init]) => String(input).endsWith('/source') && init?.method === 'PUT')!;
    const config = fetcher.mock.calls.find(([input]) => String(input).endsWith('/config'))!;
    expect(JSON.parse(String(source[1]?.body)).base_hash).toBe('source-1');
    expect(JSON.parse(String(config[1]?.body)).base_hash).toBe('desired-2');
  });

  it('上传读取 File.text 并发送 JSON，双击只安装一次', async () => {
    const upload = deferred<Response>();
    const fetcher = stubApi(catalog([]), (url, init) => url === '/v1/admin/plugins' && init?.method === 'POST' ? upload.promise : undefined);
    render(<PluginsSettingsPage />);
    await screen.findByText('尚无插件，请导入 .py 文件');
    const file = new File(['contents'], 'new_plugin.py', { type: 'text/x-python' });
    Object.defineProperty(file, 'text', { value: vi.fn().mockResolvedValue('PLUGIN_META = {}') });
    fireEvent.change(screen.getByLabelText('导入插件文件'), { target: { files: [file] } });
    const button = screen.getByRole('button', { name: '导入插件' });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1));
    const call = fetcher.mock.calls.find(([, init]) => init?.method === 'POST')!;
    expect(JSON.parse(String(call[1]?.body))).toEqual({ filename: 'new_plugin.py', content: 'PLUGIN_META = {}' });
    expect(button).toBeDisabled();
    await act(async () => upload.resolve(response(plugin())));
    await screen.findByText(/插件已导入，默认停用/);
    expect(screen.getByLabelText('启用插件')).not.toBeChecked();
  });

  it('更新源码改变默认参数时同步无修改的配置，后续保存使用新指纹', async () => {
    const next = plugin({ config: { prefix: 'new-default' }, source_hash: 'source-2', desired_hash: 'desired-2' });
    const fetcher = stubApi(catalog(), (url, init) => {
      if (url.endsWith('/source') && init?.method === 'PUT') return response(next);
      if (url.endsWith('/config')) return response(next);
      return undefined;
    });
    await openPage();
    fireEvent.change(screen.getByLabelText('插件 Python 源码'), { target: { value: 'PLUGIN_META = {"params_schema": []}' } });
    fireEvent.click(screen.getByRole('button', { name: '保存源码' }));
    await screen.findByText(/源码已保存，运行进程会自动更新/);
    expect(screen.getByLabelText('前缀')).toHaveValue('new-default');
    fireEvent.click(screen.getByLabelText('启用插件'));
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(fetcher.mock.calls.some(([input]) => String(input).endsWith('/config'))).toBe(true));
    expect(JSON.parse(String(fetcher.mock.calls.find(([input]) => String(input).endsWith('/config'))![1]?.body))).toMatchObject({ base_hash: 'desired-2', config: { prefix: 'new-default' } });
  });

  it('源码无法解码时仍可用文件替换修复，并保留原文件指纹', async () => {
    const fetcher = stubApi(catalog(), (url, init) => {
      if (!url.endsWith('/source')) return undefined;
      return init?.method === 'PUT' ? response(plugin({ source_hash: 'source-2', desired_hash: 'desired-2' })) : response({ detail: '源码无法解码' }, 400);
    });
    render(<PluginsSettingsPage />);
    await screen.findByText(/源码无法解码/);
    const file = new File([''], 'fixed.py');
    Object.defineProperty(file, 'text', { value: () => Promise.resolve('PLUGIN_META = {}\n') });
    fireEvent.change(screen.getByLabelText('替换插件文件'), { target: { files: [file] } });
    await screen.findByLabelText('插件 Python 源码');
    fireEvent.click(screen.getByRole('button', { name: '保存源码' }));
    await screen.findByText(/源码已保存，运行进程会自动更新/);
    const call = fetcher.mock.calls.find(([url, init]) => String(url).endsWith('/source') && init?.method === 'PUT')!;
    expect(JSON.parse(String(call[1]?.body))).toEqual({ content: 'PLUGIN_META = {}\n', base_hash: 'source-1' });
  });

  it('源码返回不同配置时保留已有参数草稿并明确提示重新核对', async () => {
    const next = plugin({ config: { prefix: 'external' }, source_hash: 'source-2', desired_hash: 'desired-2' });
    const fetcher = stubApi(catalog(), (url, init) => {
      if (url.endsWith('/source') && init?.method === 'PUT') return response(next);
      if (url.endsWith('/config')) return response({ detail: 'changed' }, 409);
      return undefined;
    });
    await openPage();
    fireEvent.change(screen.getByLabelText('前缀'), { target: { value: 'draft' } });
    fireEvent.change(screen.getByLabelText('插件 Python 源码'), { target: { value: 'PLUGIN_META = {"params_schema": []}' } });
    fireEvent.click(screen.getByRole('button', { name: '保存源码' }));
    await screen.findByText(/源码已保存，但参数定义或配置已变化/);
    expect(screen.getByLabelText('前缀')).toHaveValue('draft');
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await screen.findByText(/插件已被其他页面修改/);
    expect(JSON.parse(String(fetcher.mock.calls.find(([input]) => String(input).endsWith('/config'))![1]?.body)).base_hash).toBe('desired-1');
  });

  it('应用只请求热加载，按进程反馈显示错误且把错误作为文本呈现', async () => {
    const data = catalog();
    data.runtime[0].plugins = [{ id: 'request_filter', desired_hash: 'desired-1', status: 'error', error: '<img src=x onerror=alert(1)>', errors: [{ phase: 'hook', message: 'hook failed', at: 'now' }] }];
    const fetcher = stubApi(catalog(), (url) => url.endsWith('/apply') ? response({ generation: 'generation-1', runtime: data.runtime, message: '已发出刷新请求' }) : undefined);
    await openPage();
    fireEvent.click(screen.getByRole('button', { name: '应用已保存的更改' }));
    await screen.findByText('已发出刷新请求');
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(screen.getByText(/hook failed/)).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST').map(([input]) => input)).toEqual(['/v1/admin/plugins/apply']);
  });

  it('仅引擎插件在 Supervisor 不适用时仍可确认已应用', async () => {
    const data = catalog([plugin({ enabled: true })]);
    data.runtime[0].plugins[0] = { id: 'request_filter', desired_hash: 'desired-1', status: 'loaded' };
    data.runtime.push({ ...data.runtime[0], pid: 456, process: 'supervisor', plugins: [{ id: 'request_filter', desired_hash: 'desired-1', status: 'not_applicable' }] });
    stubApi(data);
    await openPage();
    expect(screen.getAllByText('已应用 · 启用')).toHaveLength(2);
    expect(screen.getByText(/Supervisor|supervisor/)).toHaveTextContent('不在此进程运行');
    expect(screen.queryByText('等待进程更新 / 上报')).toBeNull();
  });

  it('适用引擎离线时不能仅凭 Supervisor 排除状态宣称已启用', async () => {
    const data = catalog([plugin({ enabled: true })]);
    data.runtime[0].alive = false;
    data.runtime[0].plugins[0].status = 'stopped';
    data.runtime.push({ ...data.runtime[0], pid: 456, process: 'supervisor', alive: true, plugins: [{ id: 'request_filter', desired_hash: 'desired-1', status: 'not_applicable' }] });
    stubApi(data);
    await openPage();
    expect(screen.getAllByText('等待适用进程上报')).toHaveLength(2);
    expect(screen.queryByText('已应用 · 启用')).toBeNull();
  });

  it('旧令牌列表请求迟到时不能替换新令牌页面', async () => {
    const first = deferred<Response>();
    stubApi(catalog([plugin({ name: '新令牌插件' })]), (url, init) => url === '/v1/admin/plugins' && (init?.headers as Record<string, string>)?.Authorization === 'Bearer token-a' ? first.promise : undefined);
    render(<PluginsSettingsPage />);
    act(() => useSettingsStore.setState({ adminToken: 'token-b' }));
    await screen.findByRole('heading', { name: '新令牌插件' });
    await act(async () => first.resolve(response(catalog([plugin({ name: '旧令牌插件' })]))));
    expect(screen.queryByText('旧令牌插件')).toBeNull();
    expect(screen.getByRole('heading', { name: '新令牌插件' })).toBeInTheDocument();
  });

  it('切换令牌后忽略旧请求，包括读取文件后迟到的上传', async () => {
    const text = deferred<string>();
    const fetcher = stubApi();
    await openPage();
    const file = new File([''], 'late.py');
    Object.defineProperty(file, 'text', { value: () => text.promise });
    fireEvent.change(screen.getByLabelText('导入插件文件'), { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: '导入插件' }));
    act(() => useSettingsStore.setState({ adminToken: 'token-b' }));
    await act(async () => text.resolve('PLUGIN_META = {}'));
    expect(fetcher.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false);
    await screen.findByLabelText('插件 Python 源码');
  });

  it('删除自定义插件带期望指纹并自动卸载', async () => {
    const fetcher = stubApi(catalog(), (_url, init) => init?.method === 'DELETE' ? response({}) : undefined);
    await openPage();
    fireEvent.click(screen.getByRole('button', { name: '删除插件' }));
    await screen.findByText(/插件已删除/);
    expect(fetcher.mock.calls.find(([, init]) => init?.method === 'DELETE')?.[0]).toBe('/v1/admin/plugins/request_filter?base_hash=desired-1');
    expect(screen.queryByLabelText('插件 Python 源码')).toBeNull();
  });

  it('待更新状态每三秒自动读取，保留草稿并在进程确认后停止', async () => {
    vi.useFakeTimers();
    let reads = 0;
    const fetcher = stubApi(catalog(), (url, init) => {
      if (url !== '/v1/admin/plugins' || init?.method) return undefined;
      reads += 1;
      const data = catalog();
      if (reads > 1) data.runtime[0].plugins[0] = { id: 'request_filter', desired_hash: 'desired-1', status: 'disabled' };
      return response(data);
    });
    await act(async () => { render(<PluginsSettingsPage />); });
    fireEvent.change(screen.getByLabelText('前缀'), { target: { value: 'unsaved' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(2999); });
    expect(reads).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(reads).toBe(2);
    expect(screen.getByLabelText('前缀')).toHaveValue('unsaved');
    expect(screen.getAllByText('已应用 · 停用')).toHaveLength(2);
    expect(screen.getByRole('button', { name: '保存配置' })).toBeEnabled();
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(fetcher.mock.calls.filter(([url]) => String(url) === '/v1/admin/plugins')).toHaveLength(2);
  });

  it('后台状态请求未完成时不重叠，令牌切换取消请求并忽略迟到结果', async () => {
    vi.useFakeTimers();
    const pending = deferred<Response>();
    let reads = 0;
    let pollingSignal: AbortSignal | undefined;
    stubApi(catalog(), (url, init) => {
      if (url !== '/v1/admin/plugins' || init?.method) return undefined;
      reads += 1;
      if (reads === 2) { pollingSignal = init?.signal as AbortSignal; return pending.promise; }
      const data = catalog([plugin({ name: reads > 2 ? '新令牌插件' : '请求过滤' })]);
      if (reads > 2) data.runtime[0].alive = false;
      return response(data);
    });
    await act(async () => { render(<PluginsSettingsPage />); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(reads).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(reads).toBe(2);
    await act(async () => { useSettingsStore.setState({ adminToken: 'token-b' }); });
    expect(pollingSignal?.aborted).toBe(true);
    const stale = catalog();
    stale.runtime[0].plugins[0] = { id: 'request_filter', desired_hash: 'desired-1', status: 'error', error: '旧令牌错误' };
    await act(async () => { pending.resolve(response(stale)); });
    expect(screen.getByRole('heading', { name: '新令牌插件' })).toBeInTheDocument();
    expect(screen.queryByText('旧令牌错误')).toBeNull();
  });

  it('保存新版本后重新轮询，并等旧请求结束才停止', async () => {
    vi.useFakeTimers();
    let reads = 0;
    let saved = false;
    const next = plugin({ enabled: true, desired_hash: 'desired-2' });
    stubApi(catalog(), (url, init) => {
      if (url.endsWith('/config')) { saved = true; return response(next); }
      if (url !== '/v1/admin/plugins' || init?.method) return undefined;
      reads += 1;
      const data = catalog([saved ? next : plugin()]);
      data.runtime[0].plugins[0] = { id: 'request_filter', desired_hash: saved ? 'desired-2' : 'desired-1', status: saved ? 'loaded' : 'disabled', active_requests: saved && reads === 2 ? 1 : 0 };
      return response(data);
    });
    await act(async () => { render(<PluginsSettingsPage />); });
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(reads).toBe(1);
    fireEvent.click(screen.getByLabelText('启用插件'));
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '保存配置' })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(reads).toBe(2);
    expect(screen.getByText('1 个请求仍在使用此插件')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(reads).toBe(3);
    expect(screen.queryByText('1 个请求仍在使用此插件')).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(reads).toBe(3);
  });

  it.each(['error', 'offline'] as const)('%s 状态终止自动轮询', async (state) => {
    vi.useFakeTimers();
    const data = catalog();
    data.runtime[0].alive = state !== 'offline';
    data.runtime[0].plugins[0] = { id: 'request_filter', desired_hash: 'desired-1', status: state === 'error' ? 'error' : 'stopped' };
    const fetcher = stubApi(data);
    await act(async () => { render(<PluginsSettingsPage />); });
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(fetcher.mock.calls.filter(([url]) => String(url) === '/v1/admin/plugins')).toHaveLength(1);
  });
});
