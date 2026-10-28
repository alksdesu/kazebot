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

  // 拿 null 顶上，保存时不带指纹，真文件被整份盖掉；拿空串顶上，编辑器摆出一份空文件，照着存就清空了。
  it.each([
    [{ content: 'a: 1\n' }, '缺 hash'],
    [{ content: 'a: 1\n', hash: null }, 'hash 不是字符串'],
    [{ content: 'a: 1\n', hash: 12345 }, 'hash 不是字符串'],
    [{ hash: 'h1' }, '缺 content'],
    [{ content: null, hash: 'h1' }, 'content 不是字符串'],
    [[], '返回的不是 JSON 对象'],
  ])('正文或指纹对不上形状就报格式不对，不拿空串、null 顶上（%j）', async (body, problem) => {
    serveRead(CONFIG, body);

    await expect(api.getConfigRaw(TOKEN)).rejects.toThrow(`models.yaml 原文格式不对：${problem}`);
  });

  it('文件还不存在时后端给空串，那也是一份指纹', async () => {
    serveRead(CONFIG, { content: '', hash: '' });

    await expect(api.getConfigRaw(TOKEN)).resolves.toEqual({ content: '', hash: '' });
  });

  it('报错说的是哪一份文件', async () => {
    const cases: Array<[string, () => Promise<unknown>, string]> = [
      [RUNTIME, () => api.getRuntimeRaw(TOKEN), 'runtime.yaml 原文'],
      ['/admin/config/policy/raw', () => api.getPolicyRaw(TOKEN), 'policy.yaml 原文'],
      ['/admin/config/schedules/raw', () => api.getSchedulesRaw(TOKEN), 'schedules.yaml 原文'],
      ['/admin/config/mcp-clients/raw', () => api.getMcpClientsRaw(TOKEN), 'mcp.yaml 原文'],
      ['/admin/config/nodes/a%20b/raw', () => api.getNodeRaw(TOKEN, 'a b'), '节点 a b 的原文'],
      ['/admin/config/node-files/x.yaml/raw', () => api.getNodeFileRaw(TOKEN, 'x.yaml'), '节点文件 x.yaml 的原文'],
      ['/admin/config/tools/t/raw', () => api.getToolRaw(TOKEN, 't'), '工具 t 的源码'],
      ['/admin/config/skills/s/raw', () => api.getSkillRaw(TOKEN, 's'), '技能 s 的原文'],
      ['/admin/config/drawtools/settings/raw', () => api.getDrawtoolsSettingsRaw(TOKEN), '绘图 settings.yaml 原文'],
      ['/admin/config/drawtools/characters/raw', () => api.getDrawtoolsCharactersRaw(TOKEN), '绘图 character_tags.yaml 原文'],
      ['/admin/config/drawtools/prompts/top-system/raw', () => api.getDrawtoolsPromptRaw(TOKEN, 'top-system'), '绘图提示词 top-system 的原文'],
    ];
    for (const [path] of cases) serveRead(path, { content: 'x: 1\n' });

    for (const [, read, what] of cases) await expect(read()).rejects.toThrow(`${what}格式不对：缺 hash`);
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

  it('还没接管的平台配置给的是空正文、空指纹、exists 为假', async () => {
    serveRead('/platforms/qq/config/raw', { content: '', hash: '', exists: false });

    await expect(api.getPlatformRaw(TOKEN, 'qq')).resolves.toEqual({ content: '', hash: '', exists: false });
  });

  // 缺了 exists，「还没接管」和「接管了但是空的」就分不开；缺了指纹，结构化写回不带基准。
  it.each([
    ['/platforms/qq/config/raw', () => api.getPlatformRaw(TOKEN, 'qq'), '平台 qq 的配置原文'],
    ['/features/config/raw', () => api.getFeaturesRaw(TOKEN), 'features.yaml 原文'],
  ])('平台和功能配置缺了指纹或 exists 都报格式不对（%s）', async (path, read, what) => {
    serveRead(path, { content: 'x: 1\n', exists: true });
    await expect(read()).rejects.toThrow(`${what}格式不对：缺 hash`);

    serveRead(path, { content: 'x: 1\n', hash: 'h1' });
    await expect(read()).rejects.toThrow(`${what}格式不对：缺 exists`);

    serveRead(path, { content: 'x: 1\n', hash: 'h1', exists: 'yes' });
    await expect(read()).rejects.toThrow(`${what}格式不对：exists 不是 true 或 false`);
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

  // 回执是存完才发的：拿 null 当下一次的基准，保存时不带指纹，别处的改动会被整份盖掉；当成失败，人会再存一次。
  it.each([
    [{ ok: true }, 'models.yaml 已经存上，可回执格式不对：缺 hash'],
    [{ ok: true, hash: null }, 'models.yaml 已经存上，可回执格式不对：hash 不是字符串'],
    [{ hash: 'h2' }, 'models.yaml 已经存上，可回执格式不对：缺 ok'],
    [{ ok: true, hash: 'h2', warnings: 'x' }, 'models.yaml 已经存上，可回执格式不对：warnings 不是字符串数组'],
  ])('回执对不上形状时抛 ReceiptUnreadable，说清已经存上（%j）', async (body, message) => {
    serveWrite(CONFIG, body);

    const failure = await api.updateConfigRaw(TOKEN, 'a: 2\n', 'h1').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(api.ReceiptUnreadable);
    expect((failure as Error).message).toBe(message);
  });

  it('回执不是 JSON 也算回执读不懂，不算没存上', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>', { status: 200 })));

    const failure = await api.updateConfigRaw(TOKEN, 'a: 2\n', 'h1').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(api.ReceiptUnreadable);
    expect((failure as Error).message).toBe('models.yaml 已经存上，可回执不是 JSON');
  });

  it.each([
    ['节点 n', () => api.createNode(TOKEN, { id: 'n', content: 'id: n\n' }), '/admin/config/nodes'],
    ['工具 t', () => api.createTool(TOKEN, { id: 't', content: 'print(1)\n' }), '/admin/config/tools'],
    ['技能 s', () => api.createSkill(TOKEN, { id: 's', content: '# s\n' }), '/admin/config/skills'],
  ])('新建文件的回执读不懂时说清%s已经建好', async (what, create, path) => {
    routes[key('POST', `/v1${path}`)] = { ok: true };

    const failure = await create().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(api.ReceiptUnreadable);
    expect((failure as Error).message).toBe(`${what} 已经建好，可回执格式不对：缺 hash`);
  });

  it('新建文件的回执带着落盘那一版的指纹和提醒', async () => {
    routes[key('POST', '/v1/admin/config/nodes')] = { ok: true, hash: 'n1', path: 'config/nodes/n.yaml', warnings: ['没人读 foo'] };

    await expect(api.createNode(TOKEN, { id: 'n', content: 'id: n\n' }))
      .resolves.toEqual({ ok: true, hash: 'n1', warnings: ['没人读 foo'] });
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

  // 平台和全局设置的回执总带 warnings：缺了它就分不出「没有提醒」和「提醒没送到」。
  it.each([
    [{ ok: true, warnings: [] }, '平台 qq 的配置已经存上，可回执格式不对：缺 hash'],
    [{ ok: true, hash: 'q2' }, '平台 qq 的配置已经存上，可回执格式不对：缺 warnings'],
  ])('平台配置的回执对不上形状时抛 ReceiptUnreadable（%j）', async (body, message) => {
    serveWrite('/platforms/qq/config/raw', body);

    const failure = await api.updatePlatformRaw(TOKEN, 'qq', 'qq: {a: 1}\n', 'q1').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(api.ReceiptUnreadable);
    expect((failure as Error).message).toBe(message);
  });

  it('全局设置的回执读不懂时说的是 features.yaml', async () => {
    serveWrite('/features/config/raw', { ok: true, warnings: [] });

    await expect(api.updateFeaturesRaw(TOKEN, 'vision: {}\n', 'f1'))
      .rejects.toThrow('features.yaml 已经存上，可回执格式不对：缺 hash');
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
    for (const [path] of cases) serveWrite(path, { ok: true, hash: 'next' });
    for (const [, call] of cases) await expect(call()).resolves.toMatchObject({ hash: 'next' });

    for (const [path] of cases) expect(writes(path)[0]).toMatchObject({ base_hash: 'b' });
    expect(sent.filter(call => call.method === 'PUT')).toHaveLength(cases.length);
  });
});

describe('服务端策略也按指纹保存', () => {
  it('读回来的是策略连指纹', async () => {
    serveRead('/config/policy', { policy: { version: 1 }, hash: 'p1' });

    await expect(api.getPolicy(TOKEN)).resolves.toEqual({ policy: { version: 1 }, hash: 'p1' });
  });

  // 补成空文档的话，页面会把每一类都摆成「没有规则」，照着存一次就把整份策略清空了。
  it.each([
    [{}, '缺 policy'],
    [{ policy: [], hash: 'p1' }, 'policy 不是 JSON 对象'],
    [{ policy: { version: 1 } }, '缺 hash'],
    [{ policy: { version: 1 }, hash: null }, 'hash 不是字符串'],
  ])('读回来对不上形状就报格式不对，不给一份空文档（%j）', async (body, problem) => {
    serveRead('/config/policy', body);

    await expect(api.getPolicy(TOKEN)).rejects.toThrow(`服务端策略格式不对：${problem}`);
  });

  it('策略文件还不存在时指纹是空串，照常读', async () => {
    serveRead('/config/policy', { policy: { version: 1 }, hash: '' });

    await expect(api.getPolicy(TOKEN)).resolves.toEqual({ policy: { version: 1 }, hash: '' });
  });

  // 回执是存完才发的：读不懂也得让页面知道已经存上了，免得当成失败再存一次。
  it.each([
    [{ policy: { version: 2 } }, '策略已经存上，可回执格式不对：缺 hash'],
    [{ hash: 'p2' }, '策略已经存上，可回执格式不对：缺 policy'],
  ])('保存的回执对不上形状时抛 ReceiptUnreadable，说清已经存上（%j）', async (body, message) => {
    routes[key('PUT', '/v1/config/policy')] = body;

    const failure = await api.updatePolicy(TOKEN, { version: 2 }, 'p1').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(api.ReceiptUnreadable);
    expect((failure as Error).message).toBe(message);
  });

  it('保存被拒（409）是请求失败，不是回执读不懂', async () => {
    statuses[key('PUT', '/v1/config/policy')] = 409;
    routes[key('PUT', '/v1/config/policy')] = { detail: 'policy.yaml 已被别处改过，刷新后再改' };

    const failure = await api.updatePolicy(TOKEN, { version: 2 }, 'p1').catch((error: unknown) => error);

    expect(api.isEditConflict(failure)).toBe(true);
    expect(failure).not.toBeInstanceOf(api.ReceiptUnreadable);
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
  // 判错只看响应的状态码：报错文字里恰好带着 409 的（网关拼错、上游转述）都不算。
  it('服务器回 409 才算', () => {
    expect(api.isEditConflict(new api.ApiError(409, 'config.yaml 已被别处改过，刷新后再改'))).toBe(true);
    expect(api.isEditConflict(new api.ApiError(409, ''))).toBe(true);
  });

  it.each(['409', '409 config.yaml 已被别处改过，刷新后再改', '4090 网关把状态码拼错了', '500 上游返回 409'])('只是文字里带着 409 不算：%s', message => {
    expect(api.isEditConflict(new Error(message))).toBe(false);
  });

  it('别的状态码不算', () => {
    expect(api.isEditConflict(new api.ApiError(500, '上游返回 409'))).toBe(false);
    expect(api.isEditConflict(new api.ApiError(404, ''))).toBe(false);
  });

  it('不是 Error 的一律不算', () => {
    expect(api.isEditConflict('409')).toBe(false);
    expect(api.isEditConflict({ status: 409 })).toBe(false);
    expect(api.isEditConflict(null)).toBe(false);
  });

  it('后端判冲突时 apiFetch 把状态码和原因一起抛出来', async () => {
    statuses[key('PUT', `/v1${CONFIG}`)] = 409;
    routes[key('PUT', `/v1${CONFIG}`)] = { detail: 'config.yaml 已被别处改过，刷新后再改' };

    const failure = await api.updateConfigRaw(TOKEN, 'a: 2\n', 'h1').catch(error => error);

    expect(api.isEditConflict(failure)).toBe(true);
    expect(failure).toMatchObject({ status: 409, detail: 'config.yaml 已被别处改过，刷新后再改' });
    expect(String(failure.message)).toBe('409 config.yaml 已被别处改过，刷新后再改');
  });

  it('保存校验不过的 400 原样带回原因，什么都没写', async () => {
    statuses[key('PUT', `/v1${RUNTIME}`)] = 400;
    routes[key('PUT', `/v1${RUNTIME}`)] = { detail: 'engine 要写成映射' };

    const failure = await api.updateRuntimeRaw(TOKEN, 'engine: 1\n', 'r1').catch(error => error);

    expect(failure).toMatchObject({ status: 400, detail: 'engine 要写成映射', message: '400 engine 要写成映射' });
  });

  it('保存通过但带着提醒时，回执里一条不少', async () => {
    serveWrite('/admin/config/schedules/raw', { ok: true, hash: 's2', warnings: ['第 3 条没有 id，调度时会跳过'] });

    await expect(api.updateSchedulesRaw(TOKEN, 'schedules: []\n', 's1'))
      .resolves.toEqual({ ok: true, hash: 's2', warnings: ['第 3 条没有 id，调度时会跳过'] });
  });

  it('新建节点的回执也带着提醒', async () => {
    routes[key('POST', '/v1/admin/config/nodes')] = { ok: true, hash: 'n1', path: 'config/nodes/x.yaml', warnings: ['这些键没有任何地方读：foo'] };

    await expect(api.createNode(TOKEN, { id: 'x', content: 'id: x\nfoo: 1\n' }))
      .resolves.toEqual({ ok: true, hash: 'n1', warnings: ['这些键没有任何地方读：foo'] });
  });
});

const FIRST = [
  'shell:',
  '  entry_node_id: qq.orchestrator',
  'routing:',
  '  vision:',
  '    enabled: auto',
  '',
].join('\n');

const jsonReply = (body: unknown, status = 200): Response => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});

/**
 * 真后端那套：保存时指纹对不上就 409，一个字节都不写，存一次指纹换一版。
 * receipt 给了就拿它当存完的回执；rereadFails 时存过之后再读 runtime 都回 500。
 */
const serveRuntime = ({ receipt, rereadFails = false }: { receipt?: unknown; rereadFails?: boolean } = {}) => {
  let content = FIRST;
  let version = 1;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? 'GET').toUpperCase();
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    sent.push({ url, method, body });
    const hash = `r${version}`;
    if (url.endsWith(`/v1${RUNTIME}`) && method === 'PUT') {
      if (body.base_hash !== hash) return jsonReply({ detail: 'runtime.yaml 已被别处改过，刷新后再改' }, 409);
      content = body.content;
      version += 1;
      return jsonReply(receipt ?? { ok: true, hash: `r${version}` });
    }
    if (url.endsWith(`/v1${RUNTIME}`)) {
      if (rereadFails && version > 1) return jsonReply({ detail: '磁盘忙' }, 500);
      return jsonReply({ content, hash, source: 'override', path: 'config/runtime.yaml' });
    }
    return jsonReply({});
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

const saveRuntime = (content: string) => {
  fireEvent.change(screen.getByLabelText('runtime.yaml YAML 编辑器'), { target: { value: content } });
  fireEvent.click(within(runtimeSection()).getByRole('button', { name: '保存运行时配置' }));
};

// 同一份 runtime.yaml 两个编辑器同时开着：指纹各记各的，一边存完不会顺手把另一边的底子换新。
describe('一份文件上的两个编辑器', () => {
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

// 回执读不懂时文件已经写下去了：说成保存失败，人会再存一次；手上的指纹不换，下一次保存就撞冲突。
describe('原文编辑器保存的回执读不懂', () => {
  beforeEach(() => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    useSettingsStore.setState({ adminToken: TOKEN, isAuthenticated: true });
  });

  it('照存上了收尾，重读一遍换上落盘那一版的指纹', async () => {
    serveRuntime({ receipt: { ok: true } });
    render(<AdvancedSettingsPage />);
    await openRuntime();

    saveRuntime(`${FIRST}engine:\n  max_workers: 8\n`);

    expect(await screen.findByText('已保存')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('runtime.yaml 已经存上，可回执格式不对：缺 hash');
    expect(screen.queryByText(/保存失败/)).toBeNull();
    await waitFor(() => expect(sent.filter(call => call.method === 'GET' && call.url === `/v1${RUNTIME}`)).toHaveLength(2));

    saveRuntime(`${FIRST}engine:\n  max_workers: 9\n`);

    await waitFor(() => expect(writes(RUNTIME).map(body => body.base_hash)).toEqual(['r1', 'r2']));
  });

  it('重读也没成就留着旧指纹，交代先点「加载」再改', async () => {
    serveRuntime({ receipt: { ok: true }, rereadFails: true });
    render(<AdvancedSettingsPage />);
    await openRuntime();

    saveRuntime(`${FIRST}engine:\n  max_workers: 8\n`);

    expect(await screen.findByText('已保存')).toBeInTheDocument();
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('runtime.yaml 已经存上，可回执格式不对：缺 hash');
    expect(alert).toHaveTextContent('点「加载」读入最新内容再改，不然再存会被当成冲突拒掉');
    expect(alert).toHaveTextContent('刚才重新读取也没成：500 磁盘忙');
    expect(screen.getByLabelText('runtime.yaml YAML 编辑器')).toHaveValue(`${FIRST}engine:\n  max_workers: 8\n`);
  });
});
