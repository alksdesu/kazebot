import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState(null, '', '/web/');
});

describe('实例地址与最近入口', () => {
  it.each(['', '/i/miaomiao', '/web/1'])('识别挂载 %s，保留设置位置并移除凭据与入口标记', async path => {
    window.history.replaceState(null, '', `${path}/web/?token=private&entry=instances&view=settings&tab=providers&section=base&keep=1`);
    const { MOUNT, instanceConsoleHref } = await import('../instances/paths');
    const { instanceEntryHref, isInstanceEntry } = await import('../instances/navigation');
    expect(MOUNT).toBe(path);
    expect(isInstanceEntry()).toBe(true);
    expect(instanceEntryHref()).toBe(`${path}/web/?entry=instances`);
    expect(instanceConsoleHref('/i/next')).toBe('/i/next/web/?view=settings&tab=providers&section=base&keep=1');
    expect(instanceConsoleHref('/i/next', false)).toBe('/i/next/web/');
  });

  it.each(['https://external.example', '//external.example', '/i/../main', '/i/./main', '/i/%2e%2e', '/i/a\\b', '/i/a?token=x', '/i/a#x', '/i/a/', '/i//a', 'i/a', '/i/中文'])('拒绝非实例路径 %s', async path => {
    const { instanceConsoleHref, isInstancePath } = await import('../instances/paths');
    const { rememberInstance, getRecentInstance } = await import('../instances/navigation');
    expect(isInstancePath(path)).toBe(false);
    expect(() => instanceConsoleHref(path)).toThrow('实例地址无效');
    rememberInstance(path);
    expect(getRecentInstance()).toBeNull();
  });

  it.each(['/i/legacy.v1', '/foo~bar'])('保留自定义挂载路径 %s', async path => {
    const { instanceConsoleHref } = await import('../instances/paths');
    expect(instanceConsoleHref(path, false)).toBe(`${path}/web/`);
  });

  it('主实例的空路径与尚无最近记录有区别', async () => {
    const { getRecentInstance, rememberInstance } = await import('../instances/navigation');
    expect(getRecentInstance()).toBeNull();
    rememberInstance('');
    expect(getRecentInstance()).toBe('');
    rememberInstance('/i/miaomiao');
    expect(getRecentInstance()).toBe('/i/miaomiao');
  });

  it('存储被禁用时仍允许切换，记录不包含令牌', async () => {
    const assign = vi.fn();
    const actual = window;
    actual.history.replaceState(null, '', '/web/?token=secret');
    vi.stubGlobal('window', { location: { pathname: '/web/', get search() { return actual.location.search; }, hash: '', assign }, history: actual.history });
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('disabled'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disabled'); });
    const { navigateToInstance, getRecentInstance } = await import('../instances/navigation');
    expect(getRecentInstance()).toBeNull();
    expect(navigateToInstance('/i/miaomiao')).toBe(true);
    expect(assign).toHaveBeenCalledWith('/i/miaomiao/web/');
  });

  it('整页切换沿用浏览器离开提醒，不提前覆盖最近记录', async () => {
    const assign = vi.fn();
    const confirm = vi.fn();
    localStorage.setItem('clonoth_recent_instance', '');
    vi.stubGlobal('window', { location: { pathname: '/web/', search: '', assign }, confirm });
    const { navigateToInstance, rememberInstance, getRecentInstance } = await import('../instances/navigation');
    expect(navigateToInstance('/i/miaomiao')).toBe(true);
    expect(assign).toHaveBeenCalledWith('/i/miaomiao/web/');
    expect(confirm).not.toHaveBeenCalled();
    expect(getRecentInstance()).toBe('');
  });
});

describe('公开实例目录请求', () => {
  const directory = { current_path: '', instances: [{ label: '主实例', path: '' }, { label: 'miaomiao', path: '/i/miaomiao' }] };

  it('请求不携带管理员密钥和 Cookie，响应仅保留导航字段', async () => {
    window.history.replaceState(null, '', '/web/');
    const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ ...directory, instances: [
      { ...directory.instances[0], account: 'private', token: 'private' }, directory.instances[1],
    ] }), { headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetch);
    const { getInstanceDirectory } = await import('../api/supervisorClient');
    expect(await getInstanceDirectory()).toEqual(directory);
    expect(fetch).toHaveBeenCalledWith('/v1/instance-directory', expect.objectContaining({ credentials: 'omit', cache: 'no-store' }));
    expect(fetch.mock.calls[0][1]).not.toHaveProperty('headers');
  });

  it.each([
    { ...directory, current_path: '/i/other' },
    { ...directory, instances: [] },
    { ...directory, instances: [{ label: '外部', path: '//external.example' }] },
    { ...directory, instances: [{ label: '', path: '' }] },
  ])('目录异常不会当作单实例正常结果', async body => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body))));
    const { getInstanceDirectory } = await import('../api/supervisorClient');
    await expect(getInstanceDirectory()).rejects.toThrow(/实例列表/);
  });

  it('服务失败保留状态码供界面显示和重试', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: '实例目录暂不可用' }), { status: 503 })));
    const { getInstanceDirectory } = await import('../api/supervisorClient');
    await expect(getInstanceDirectory()).rejects.toThrow('503 实例目录暂不可用');
  });
});
