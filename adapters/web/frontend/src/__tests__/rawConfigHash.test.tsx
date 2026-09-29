// 保存 raw 配置时交回读到它的那一版指纹，后端据此认出文件中途被别处改过。指纹是各个编辑器
// 自己记的：漏带就变成整份覆盖，串了别人的就更糟 —— 共享组里两个实例互相盖掉配置，界面一声不吭。
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import { AdvancedSettingsPage } from '../components/settings/pages/AdvancedSettingsPage';
import { VisionRouting } from '../console/VisionRouting';
import { useSettingsStore } from '../store/settingsStore';

const TOKEN = 'admin-token';
const AUTH = { Authorization: `Bearer ${TOKEN}` };

interface Sent { url: string; method: string; body: any }

let sent: Sent[];
let routes: Record<string, unknown>;
let statuses: Record<string, number>;

const key = (method: string, url: string): string => `${method} ${url}`;

const serveRead = (path: string, json: unknown): void => { routes[key('GET', `/v1${path}`)] = json; };
const serveWrite = (path: string, json: unknown): void => { routes[key('PUT', `/v1${path}`)] = json; };

const writes = (path: string): any[] => sent
  .filter(call => call.method === 'PUT' && call.url === `/v1${path}`)
  .map(call => call.body);

beforeEach(() => {
  sent = [];
  routes = {};
  statuses = {};
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? 'GET').toUpperCase();
    sent.push({ url, method, body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined });
    const at = key(method, url);
    return new Response(JSON.stringify(routes[at] ?? {}), {
      status: statuses[at] ?? 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }));
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const CONFIG = '/admin/config/config/raw';
const RUNTIME = '/admin/config/runtime/raw';

describe('读回来的原文带着指纹', () => {
  it('后端给了就原样带出来', async () => {
    serveRead(CONFIG, { content: 'a: 1\n', hash: 'h1', path: 'config/config.yaml' });

    await expect(api.getConfigRaw(TOKEN)).resolves.toEqual({ content: 'a: 1\n', hash: 'h1' });
    expect(fetch).toHaveBeenCalledExactlyOnceWith(`/v1${CONFIG}`, { headers: AUTH, signal: expect.any(AbortSignal) });
  });

  it('老后端不给指纹就是 null，保存时照旧整份覆盖', async () => {
    serveRead(CONFIG, { content: 'a: 1\n' });

    await expect(api.getConfigRaw(TOKEN)).resolves.toEqual({ content: 'a: 1\n', hash: null });
  });

  it('指纹不是字符串一律当没有', async () => {
    serveRead(CONFIG, { content: 'a: 1\n', hash: 12345 });

    await expect(api.getConfigRaw(TOKEN)).resolves.toEqual({ content: 'a: 1\n', hash: null });
  });

  it('文件还不存在时后端给空串，那也是一份指纹', async () => {
    serveRead(CONFIG, { content: '', hash: '' });

    await expect(api.getConfigRaw(TOKEN)).resolves.toEqual({ content: '', hash: '' });
  });

  it('正文缺失时给空串，不把 undefined 递给编辑器', async () => {
    serveRead(CONFIG, { hash: 'h1' });

    await expect(api.getConfigRaw(TOKEN)).resolves.toEqual({ content: '', hash: 'h1' });
  });

  it('runtime 连来源、路径和指纹一起读回来', async () => {
    serveRead(RUNTIME, { content: 'x: 1\n', hash: 'r1', source: 'override', path: 'config/runtime.yaml' });

    await expect(api.getRuntimeFile(TOKEN)).resolves.toEqual({
      content: 'x: 1\n', hash: 'r1', source: 'override', path: 'config/runtime.yaml',
    });
  });

  it('平台配置读回来还带着「接管了没有」', async () => {
    serveRead('/platforms/qq/config/raw', { content: 'qq: {}\n', hash: 'q1', exists: true });

    await expect(api.getPlatformRaw(TOKEN, 'qq')).resolves.toEqual({ content: 'qq: {}\n', hash: 'q1', exists: true });
  });

  it('功能配置走 /features/config/raw', async () => {
    serveRead('/features/config/raw', { content: 'vision: {}\n', hash: 'f1', exists: true });

    await expect(api.getFeaturesRaw(TOKEN)).resolves.toEqual({ content: 'vision: {}\n', hash: 'f1', exists: true });
  });
});

describe('保存时交回指纹', () => {
  it('给了指纹就带 base_hash', async () => {
    serveWrite(CONFIG, { ok: true, hash: 'h2' });

    await expect(api.updateConfigRaw(TOKEN, 'a: 2\n', 'h1')).resolves.toEqual({ ok: true, hash: 'h2' });
    expect(writes(CONFIG)).toEqual([{ content: 'a: 2\n', base_hash: 'h1' }]);
  });

  it('没有指纹（null）就不带，整份覆盖', async () => {
    serveWrite(CONFIG, { ok: true, hash: 'h2' });

    await api.updateConfigRaw(TOKEN, 'a: 2\n', null);

    expect(writes(CONFIG)).toEqual([{ content: 'a: 2\n' }]);
  });

  it('空串指纹照样带 —— 它的意思是「保存时这份文件还得不存在」', async () => {
    serveWrite(CONFIG, { ok: true, hash: 'h2' });

    await api.updateConfigRaw(TOKEN, 'a: 1\n', '');

    expect(writes(CONFIG)).toEqual([{ content: 'a: 1\n', base_hash: '' }]);
  });

  it('保存结果里带着落盘那一版的指纹，接着存第二次就用它', async () => {
    serveWrite(CONFIG, { ok: true, hash: 'h2' });

    const first = await api.updateConfigRaw(TOKEN, 'a: 2\n', 'h1');
    await api.updateConfigRaw(TOKEN, 'a: 3\n', first.hash);

    expect(writes(CONFIG)).toEqual([
      { content: 'a: 2\n', base_hash: 'h1' },
      { content: 'a: 3\n', base_hash: 'h2' },
    ]);
  });

  it('后端不回指纹就是 null，下一次保存不拿旧的顶上', async () => {
    serveWrite(CONFIG, { ok: true });

    await expect(api.updateConfigRaw(TOKEN, 'a: 2\n', 'h1')).resolves.toEqual({ ok: true, hash: null });
  });

  it('后端明说 ok:false 才算没存上', async () => {
    serveWrite(CONFIG, { ok: false, hash: 'h2' });

    await expect(api.updateConfigRaw(TOKEN, 'a: 2\n', 'h1')).resolves.toEqual({ ok: false, hash: 'h2' });
  });

  it('平台配置的保存结果同时带指纹和告警', async () => {
    serveWrite('/platforms/qq/config/raw', { ok: true, hash: 'q2', warnings: ['bot 没在跑'] });

    await expect(api.updatePlatformRaw(TOKEN, 'qq', 'qq: {a: 1}\n', 'q1'))
      .resolves.toEqual({ ok: true, hash: 'q2', warnings: ['bot 没在跑'] });
    expect(writes('/platforms/qq/config/raw')).toEqual([{ content: 'qq: {a: 1}\n', base_hash: 'q1' }]);
  });

  it('功能配置不给指纹时同样不带 base_hash', async () => {
    serveWrite('/features/config/raw', { ok: true, hash: 'f2', warnings: [] });

    await api.updateFeaturesRaw(TOKEN, 'vision: {}\n', null);

    expect(writes('/features/config/raw')).toEqual([{ content: 'vision: {}\n' }]);
  });

  it('每一条 raw 路径都走同一套规矩', async () => {
    const cases: Array<[string, () => Promise<unknown>]> = [
      [RUNTIME, () => api.updateRuntimeRaw(TOKEN, 'x: 1\n', 'b')],
      ['/admin/config/policy/raw', () => api.updatePolicyRaw(TOKEN, 'rules: []\n', 'b')],
      ['/admin/config/schedules/raw', () => api.updateSchedulesRaw(TOKEN, 'schedules: []\n', 'b')],
      ['/admin/config/mcp-clients/raw', () => api.updateMcpClientsRaw(TOKEN, 'clients: {}\n', 'b')],
      ['/admin/config/nodes/a%20b/raw', () => api.updateNodeRaw(TOKEN, 'a b', 'id: a b\n', 'b')],
      ['/admin/config/node-files/x.yaml/raw', () => api.updateNodeFileRaw(TOKEN, 'x.yaml', 'id: x\n', 'b')],
      ['/admin/config/tools/t/raw', () => api.updateToolRaw(TOKEN, 't', 'print(1)\n', 'b')],
      ['/admin/config/skills/s/raw', () => api.updateSkillRaw(TOKEN, 's', '# s\n', 'b')],
      ['/admin/config/drawtools/settings/raw', () => api.updateDrawtoolsSettingsRaw(TOKEN, 'api: {}\n', 'b')],
      ['/admin/config/drawtools/characters/raw', () => api.updateDrawtoolsCharactersRaw(TOKEN, 'characters: []\n', 'b')],
      ['/admin/config/drawtools/prompts/top-system/raw', () => api.updateDrawtoolsPromptRaw(TOKEN, 'top-system', 'top\n', 'b')],
    ];
    for (const [, call] of cases) await call();

    for (const [path] of cases) expect(writes(path)[0]).toMatchObject({ base_hash: 'b' });
    expect(sent.filter(call => call.method === 'PUT')).toHaveLength(cases.length);
  });
});

describe('服务端策略也按指纹保存', () => {
  it('读回来的是策略连指纹', async () => {
    serveRead('/config/policy', { policy: { version: 1 }, hash: 'p1' });

    await expect(api.getPolicy(TOKEN)).resolves.toEqual({ policy: { version: 1 }, hash: 'p1' });
  });

  it('后端没给 policy 字段时给一份空文档，渲染不至于直接炸', async () => {
    serveRead('/config/policy', {});

    await expect(api.getPolicy(TOKEN)).resolves.toEqual({ policy: {}, hash: null });
  });

  it('保存带 base_hash，结果里换成新指纹', async () => {
    routes[key('PUT', '/v1/config/policy')] = { policy: { version: 2 }, hash: 'p2' };

    await expect(api.updatePolicy(TOKEN, { version: 2 }, 'p1')).resolves.toEqual({ policy: { version: 2 }, hash: 'p2' });
    expect(sent.filter(call => call.method === 'PUT').map(call => call.body))
      .toEqual([{ policy: { version: 2 }, base_hash: 'p1' }]);
  });

  it('没有指纹就不带', async () => {
    routes[key('PUT', '/v1/config/policy')] = { policy: {}, hash: 'p2' };

    await api.updatePolicy(TOKEN, { version: 2 }, null);

    expect(sent.filter(call => call.method === 'PUT').map(call => call.body)).toEqual([{ policy: { version: 2 } }]);
  });
});

describe('认出编辑冲突', () => {
  it.each(['409', '409 config.yaml 已被别处改过，刷新后再改'])('以 409 打头才算：%s', message => {
    expect(api.isEditConflict(new Error(message))).toBe(true);
  });

  it.each(['4090 网关把状态码拼错了', '409500', '500 上游返回 409', 'Upload failed: 409'])('%s 不算', message => {
    expect(api.isEditConflict(new Error(message))).toBe(false);
  });

  it('不是 Error 的一律不算', () => {
    expect(api.isEditConflict('409')).toBe(false);
    expect(api.isEditConflict({ message: '409' })).toBe(false);
    expect(api.isEditConflict(null)).toBe(false);
  });

  it('后端判冲突时 apiFetch 把状态码和原因一起抛出来', async () => {
    statuses[key('PUT', `/v1${CONFIG}`)] = 409;
    routes[key('PUT', `/v1${CONFIG}`)] = { detail: 'config.yaml 已被别处改过，刷新后再改' };

    const failure = await api.updateConfigRaw(TOKEN, 'a: 2\n', 'h1').catch(error => error);

    expect(api.isEditConflict(failure)).toBe(true);
    expect(String(failure.message)).toBe('409 config.yaml 已被别处改过，刷新后再改');
  });
});

// 同一份 runtime.yaml 两个编辑器同时开着：指纹各记各的，一边存完不会顺手把另一边的底子换新。
describe('一份文件上的两个编辑器', () => {
  const FIRST = [
    'shell:',
    '  entry_node_id: qq.orchestrator',
    'routing:',
    '  vision:',
    '    enabled: auto',
    '',
  ].join('\n');

  /** 真后端那套：保存时指纹对不上就 409，一个字节都不写。 */
  const serveRuntime = () => {
    let content = FIRST;
    let hash = 'r1';
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      const method = (init.method ?? 'GET').toUpperCase();
      const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
      sent.push({ url, method, body });
      if (url.endsWith(`/v1${RUNTIME}`) && method === 'PUT') {
        if (body.base_hash !== hash) {
          return new Response(JSON.stringify({ detail: 'runtime.yaml 已被别处改过，刷新后再改' }), {
            status: 409, headers: { 'Content-Type': 'application/json' },
          });
        }
        content = body.content;
        hash = 'r2';
        return new Response(JSON.stringify({ ok: true, hash }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      const payload = url.endsWith(`/v1${RUNTIME}`)
        ? { content, hash, source: 'override', path: 'config/runtime.yaml' }
        : {};
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));
  };

  const runtimeSection = () => screen.getByText('运行时配置 (runtime.yaml)').closest('details')!;

  const openRuntime = async () => {
    const element = runtimeSection();
    element.open = true;
    fireEvent(element, new Event('toggle'));
    fireEvent.click(within(element).getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('runtime.yaml YAML 编辑器')).toHaveValue(FIRST));
  };

  it('一边存完，另一边拿着自己那份旧指纹去存，撞上冲突而不是静默盖掉', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    useSettingsStore.setState({ adminToken: TOKEN, isAuthenticated: true });
    serveRuntime();
    render(<><VisionRouting /><AdvancedSettingsPage /></>);

    // 原文编辑器先读到 r1，草稿基于它改。
    await openRuntime();
    const editor = screen.getByLabelText('runtime.yaml YAML 编辑器');
    fireEvent.change(editor, { target: { value: `${FIRST}engine:\n  max_workers: 8\n` } });

    // 带图路由这一块现读现写，落盘之后文件变成 r2。
    await screen.findByText('带图消息');
    fireEvent.change(screen.getByLabelText('带图消息路由'), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: '保存路由' }));
    await screen.findByText('已保存。下一条带图消息就按新规矩走。');

    fireEvent.click(within(runtimeSection()).getByRole('button', { name: '保存运行时配置' }));

    expect(await screen.findByText('runtime.yaml 在打开之后被别处改过，没有保存：点「加载」读入最新内容，再把改动重做一遍。'))
      .toBeInTheDocument();
    // 两次保存各自带的是自己读到的那一版，原文编辑器没有被另一块换过底子。
    expect(writes(RUNTIME).map(body => body.base_hash)).toEqual(['r1', 'r1']);
    expect(editor).toHaveValue(`${FIRST}engine:\n  max_workers: 8\n`);
  });

  it('点「加载」换上最新那一版之后，同样的改动就存得下去了', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    useSettingsStore.setState({ adminToken: TOKEN, isAuthenticated: true });
    serveRuntime();
    render(<><VisionRouting /><AdvancedSettingsPage /></>);

    await openRuntime();
    await screen.findByText('带图消息');
    fireEvent.change(screen.getByLabelText('带图消息路由'), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: '保存路由' }));
    await screen.findByText('已保存。下一条带图消息就按新规矩走。');

    fireEvent.click(within(runtimeSection()).getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('runtime.yaml YAML 编辑器')).toHaveValue(
      FIRST.replace('enabled: auto', 'enabled: false'),
    ));
    fireEvent.change(screen.getByLabelText('runtime.yaml YAML 编辑器'), {
      target: { value: `${FIRST.replace('enabled: auto', 'enabled: false')}engine:\n  max_workers: 8\n` },
    });
    fireEvent.click(within(runtimeSection()).getByRole('button', { name: '保存运行时配置' }));

    expect(await screen.findByText('已保存')).toBeInTheDocument();
    expect(writes(RUNTIME).map(body => body.base_hash)).toEqual(['r1', 'r2']);
  });
});
