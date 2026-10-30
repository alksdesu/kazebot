// Supervisor API client: endpoint shapes, which requests carry the admin token, and how server errors read.
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, clearNodeFallbacks, deleteProvider, getNodes, getProviders, ReceiptUnreadable, setActiveProvider, updateFallbacks, updateNodeFallbacks, upsertProvider, postInbound, connectGlobalWS, checkHealth, getAdminState, checkAdminAuth, createInstance, decideApproval, deleteInstance, getAllToolNames, fetchActiveTasks, cancelTask, enterPlatformLoginMode, getAccountScope, getConversations, getDrawtoolsBundle, getInstanceDirectory, getInstanceProgress, getInstances, getMemoryEntries, getMemoryOverview, getPlatformAccount, getPlatformLoginQrcode, getPlatforms, getProviderProfiles, getSessionChildren, getSessionRunningTasks, listLogFiles, listUpstreamModels, migrateAccountScope, pinPlatformAccount, switchPlatformAccount } from '../api/supervisorClient';
import * as chat from '../api/supervisorClient';
import { MOUNT } from '../instances/paths';
import { useSettingsStore } from '../store/settingsStore';
import { fallbackEntry, providerBlock, providerTable } from './fixtures/providerTable';
import { instancePlanReply, instanceProgressReply, scopeMigrationReply, scopeStatusReply } from './fixtures/accountReplies';
import { adminState, configReloadReply, pendingApproval, restartReply, toolsReloadReply } from './fixtures/systemReplies';
import { dryRunReply, featuresStateReply, publishedStateReply, qqSnapshot, unpublishedStateReply } from './fixtures/consoleReplies';
import schemaSnapshot from './fixtures/consoleSchemas.json';
import { featureRequest } from '../features/client';
import { readReceipt } from '../api/shape';

function stubFetch(body: unknown = {}) {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('Supervisor API client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('exports postInbound as a function', () => {
    expect(typeof postInbound).toBe('function');
  });

  it('exports connectGlobalWS as a function', () => {
    // [2026-06-03] Why: event transport moved from session polling to one global
    // WebSocket. How: assert the public helper exists instead of the removed polling
    // helper. Purpose: API exports match the long-lived realtime architecture.
    expect(typeof connectGlobalWS).toBe('function');
  });

  it('exports checkHealth as a function', () => {
    expect(typeof checkHealth).toBe('function');
  });

  it('exports getAdminState as a function', () => {
    expect(typeof getAdminState).toBe('function');
  });

  it('exports checkAdminAuth as a function', () => {
    expect(typeof checkAdminAuth).toBe('function');
  });

  it('keeps JSON error details from the supervisor', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: '上游返回 400：API key 无效' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    })));

    await expect(getPlatformAccount('secret-token', 'qq'))
      .rejects.toThrow('502 上游返回 400：API key 无效');
  });

  it('turns HTML gateway errors into readable response text', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      '<html><head><style>.x{display:none}</style></head><body><h1>Bad gateway</h1><p>Error code: 502</p></body></html>',
      { status: 502, headers: { 'Content-Type': 'text/html; charset=UTF-8' } },
    )));

    await expect(getPlatformAccount('secret-token', 'qq'))
      .rejects.toThrow('502 Bad gateway Error code: 502');
  });

  it('exports decideApproval as a function', () => {
    expect(typeof decideApproval).toBe('function');
  });

  it('exports getAllToolNames as a function', () => {
    expect(typeof getAllToolNames).toBe('function');
  });

  it('exports fetchActiveTasks as a function', () => {
    // [AutoC 2026-06-04] Why: the System dashboard task count now opens a detail
    // modal. How: assert the API wrapper exists before implementing the client.
    // Purpose: prevent future refactors from removing the task-monitor entry point.
    expect(typeof fetchActiveTasks).toBe('function');
  });

  it('exports cancelTask as a function', () => {
    // [AutoC 2026-06-04] Why: each active-task row now owns a single-task cancel
    // button. How: assert the client wrapper exists before wiring the modal. Purpose:
    // UI code can call one typed helper instead of constructing endpoint strings.
    expect(typeof cancelTask).toBe('function');
  });

  it('fetches active task summaries with the admin bearer token', async () => {
    // [AutoC 2026-06-04] Why: active task details are served by a protected admin
    // endpoint. How: verify the wrapper calls the exact URL with the bearer token.
    // Purpose: the modal can load summaries without duplicating request plumbing.
    const fetchMock = vi.fn(async () => new Response(JSON.stringify([]), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(fetchActiveTasks('secret-token')).resolves.toEqual([]);
    // Read requests carry a deadline, so fetch receives an extra abort signal.
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/tasks/active', {
      headers: { Authorization: 'Bearer secret-token' },
      signal: expect.any(AbortSignal),
    });
  });

  it('cancels a single task with the admin bearer token', async () => {
    // The backend reads only Authorization; a second token header would just copy the secret into one more place.
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(cancelTask('secret-token', 'task-abc')).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith('/v1/tasks/task-abc/cancel', {
      method: 'POST',
      headers: { Authorization: 'Bearer secret-token' },
    });
  });

  it('fetches all approval tool names with the admin bearer token', async () => {
    // [2026-06-01] Why: approval settings now load the backend's complete tool
    // list instead of relying only on a hard-coded frontend subset. How: assert the
    // exact admin endpoint and bearer header used by the API wrapper. Purpose: the
    // settings page can stay data-driven while DEFAULT_AUTO_APPROVE_TOOLS remains a
    // local-storage default only.
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(['read_file', 'gemini_image']), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(getAllToolNames('secret-token')).resolves.toEqual(['read_file', 'gemini_image']);
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/all-tool-names', {
      headers: { Authorization: 'Bearer secret-token' },
      signal: expect.any(AbortSignal),
    });
  });
});

describe('Admin token on requests', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    useSettingsStore.setState({ adminToken: '' });
  });

  const authorization = (init?: RequestInit): string | null => new Headers(init?.headers).get('Authorization');

  it('sends the signed-in token with requests that do not pass one', async () => {
    useSettingsStore.setState({ adminToken: 'secret-token' });
    const fetchMock = stubFetch({ session_id: 'web:a', inbound_seq: 1, accepted: true });

    await postInbound({ conversation_key: 'web:a', text: 'hi' });

    expect(fetchMock.mock.calls[0][0]).toBe('/v1/inbound');
    expect(authorization(fetchMock.mock.calls[0][1])).toBe('Bearer secret-token');
  });

  it('keeps the token off the public health check and instance directory', async () => {
    useSettingsStore.setState({ adminToken: 'secret-token' });
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => new Response(JSON.stringify(
      url.endsWith('/health') ? { status: 'ok', uptime_seconds: 12 } : { current_path: MOUNT, instances: [{ label: '本实例', path: MOUNT }] },
    ), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);

    await checkHealth();
    await getInstanceDirectory();

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/v1/health', '/v1/instance-directory']);
    expect(fetchMock.mock.calls.map(([, init]) => authorization(init))).toEqual([null, null]);
  });

  it('sends nothing extra when nobody is signed in', async () => {
    const fetchMock = stubFetch({ session_id: 'web:a', inbound_seq: 1, accepted: true });

    await postInbound({ conversation_key: 'web:a', text: 'hi' });

    expect(authorization(fetchMock.mock.calls[0][1])).toBeNull();
  });
});

describe('Platform account client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // 各接口按后端真实回包答：这里看的是路径和请求体，回包读不懂会先在前面抛出来。
  const ACCOUNT_REPLIES: ReadonlyArray<readonly [RegExp, unknown]> = [
    [/\/account\/scope\/migrate$/, scopeMigrationReply()],
    [/\/account\/scope(\?|$)/, scopeStatusReply()],
    [/\/account\/qrcode$/, { qrcode: '', reachable: true }],
    [/\/account\/switch$/, { ok: true, account: '1000002', pinned: true, pin_error: '' }],
    [/\/account\/pin$/, { ok: true, account: '1000002' }],
    [/\/account\/relogin$/, { ok: true }],
    [/\/account$/, { configured: true, reachable: true, account: '1000001' }],
  ];
  const stubAccountReplies = () => {
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => new Response(
      JSON.stringify(ACCOUNT_REPLIES.find(([pattern]) => pattern.test(url))?.[1] ?? {}),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ));
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  };
  const preview = {
    source_scope: '1000001', target_scope: '2000002',
    conversations: [{ old_namespace: 'ns-old', new_namespace: 'ns-new', has_memory: true, blocked: false }],
    unknown_namespaces: [], unresolved_namespaces: ['ns-lost'], other_scopes: [{ scope: '', conversations: 2 }],
    stores: { sessions: { label: '会话注册表', moved: 3, replaced: 1 } },
  };

  it('reads the account of the addressed platform', async () => {
    const fetchMock = stubFetch({ configured: true, account: '1000001' });

    await expect(getPlatformAccount('secret-token', 'qq')).resolves.toEqual({ configured: true, account: '1000001' });
    expect(fetchMock).toHaveBeenCalledWith('/v1/platforms/qq/account', {
      headers: { Authorization: 'Bearer secret-token' },
      signal: expect.any(AbortSignal),
    });
  });

  // 读成「没配」的话，页面叫人去配 WebUI token，可那份配置本来没问题。
  it.each([
    [null, '返回的不是 JSON 对象'],
    [{ account: '1000001' }, '缺 configured'],
    [{ configured: 'yes' }, 'configured 不是 true 或 false'],
    [{ configured: true, reachable: 'no' }, 'reachable 不是 true 或 false'],
    [{ configured: true, quick_login: {} }, 'quick_login 不是数组'],
    [{ configured: true, quick_login: [{ nick: '备用号' }] }, '缺 quick_login[0].account'],
    [{ configured: true, quick_login: [{ account: '2000002', available: 'false' }] }, 'quick_login[0].available 不是 true 或 false'],
  ])('an account reply that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(getPlatformAccount('secret-token', 'qq')).rejects.toThrow(`账号状态格式不对：${problem}`);
  });

  it('reads the switchable accounts and a scope preview that match their shapes', async () => {
    const status = {
      configured: true, reachable: true, account: '1000001', nick: '当前机器人', online: true, is_login: true,
      login_error: '', qrcode: '',
      quick_login: [{ account: '2000002', nick: '备用号', avatar: '', available: false, dead_reason: '登录态失效' }],
    };
    stubFetch(status);
    await expect(getPlatformAccount('secret-token', 'qq')).resolves.toEqual(status);

    const scope = scopeStatusReply({ preview });
    stubFetch(scope);
    await expect(getAccountScope('secret-token', 'qq', '2000002')).resolves.toEqual(scope);
  });

  // 补成「还没绑定账号」「无需搬迁」的话，页面替后端下了结论。
  it.each([
    [{ ...scopeStatusReply(), current_scope: undefined }, '缺 current_scope'],
    [{ ...scopeStatusReply(), bot_alive: 'no' }, 'bot_alive 不是 true 或 false'],
    [{ ...scopeStatusReply(), preview: undefined }, '缺 preview'],
    [{ ...scopeStatusReply(), ambiguous_sources: undefined }, '缺 ambiguous_sources'],
    [{ ...scopeStatusReply(), ambiguous_sources: [{ scope: '1000001' }] }, '缺 ambiguous_sources[0].conversations'],
    [{ ...scopeStatusReply(), preview: { ...preview, conversations: undefined } }, '缺 preview.conversations'],
    [{ ...scopeStatusReply(), preview: { ...preview, unresolved_namespaces: 'ns-lost' } }, 'preview.unresolved_namespaces 不是字符串数组'],
    [
      { ...scopeStatusReply(), preview: { ...preview, stores: { sessions: { label: '会话注册表', moved: '3', replaced: 0 } } } },
      'preview.stores.sessions.moved 不是数字',
    ],
  ])('a scope reply that does not match its shape is refused (%#)', async (body, problem) => {
    stubFetch(body);

    await expect(getAccountScope('secret-token', 'qq')).rejects.toThrow(`记忆归属格式不对：${problem}`);
  });

  it.each([
    [{ reachable: true }, '缺 qrcode'],
    [{ qrcode: 'qr-payload', reachable: 'yes' }, 'reachable 不是 true 或 false'],
    [{ qrcode: '', reachable: false, reason: 503 }, 'reason 不是字符串'],
  ])('a qrcode reply that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(getPlatformLoginQrcode('secret-token', 'qq')).rejects.toThrow(`登录二维码格式不对：${problem}`);
  });

  it('reads the roster problem a switch or pin hands back', async () => {
    stubFetch({ ok: true, account: '2000002', pinned: true, pin_error: '', roster_error: ' 号已经换上，但没记进实例清单：Permission denied ' });
    await expect(switchPlatformAccount('secret-token', 'qq', '2000002'))
      .resolves.toEqual({ rosterError: '号已经换上，但没记进实例清单：Permission denied' });

    stubFetch({ ok: true, account: '2000002' });
    await expect(pinPlatformAccount('secret-token', 'qq')).resolves.toEqual({ rosterError: '' });

    stubFetch(scopeMigrationReply());
    await expect(migrateAccountScope('secret-token', 'qq', '2000002')).resolves.toEqual(scopeMigrationReply());
  });

  // 号已经换上、数据已经搬完，只是回执读不懂：报成失败的话，人会再做一次。
  it('treats a switch, pin or migration whose receipt it cannot read as done', async () => {
    stubFetch({ ok: true });
    const switched = await switchPlatformAccount('secret-token', 'qq', '2000002').catch((error: unknown) => error);
    expect(switched).toBeInstanceOf(ReceiptUnreadable);
    expect(switched).toMatchObject({ message: '已经切到 2000002，可回执格式不对：缺 account' });
    const migrated = await migrateAccountScope('secret-token', 'qq', '2000002').catch((error: unknown) => error);
    expect(migrated).toBeInstanceOf(ReceiptUnreadable);
    expect(migrated).toMatchObject({ message: '记忆归属已经搬完，可回执格式不对：缺 moved_memory_dirs' });

    stubFetch({ ok: true, account: 1000001 });
    await expect(pinPlatformAccount('secret-token', 'qq'))
      .rejects.toThrow('已经设成重启后自动登录，可回执格式不对：account 不是字符串');
  });

  it('an explicit ok false in a switch, pin or migration reply reads as not done', async () => {
    stubFetch({ ok: false, account: '' });
    await expect(switchPlatformAccount('secret-token', 'qq', '2000002')).rejects.toThrow('服务端没有切到 2000002，请重试');
    await expect(pinPlatformAccount('secret-token', 'qq')).rejects.toThrow('服务端没有设成重启后自动登录，请重试');

    stubFetch(scopeMigrationReply({ ok: false }));
    await expect(migrateAccountScope('secret-token', 'qq', '2000002')).rejects.toThrow('服务端没有搬迁，请重试');
  });

  it('escapes the platform id in every account path', async () => {
    const fetchMock = stubAccountReplies();

    await getPlatformAccount('secret-token', 'we chat/v2');
    await switchPlatformAccount('secret-token', 'we chat/v2', '1000002');
    await enterPlatformLoginMode('secret-token', 'we chat/v2');
    await pinPlatformAccount('secret-token', 'we chat/v2');
    await getPlatformLoginQrcode('secret-token', 'we chat/v2');
    await getAccountScope('secret-token', 'we chat/v2');
    await migrateAccountScope('secret-token', 'we chat/v2', '20002');

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/v1/platforms/we%20chat%2Fv2/account',
      '/v1/platforms/we%20chat%2Fv2/account/switch',
      '/v1/platforms/we%20chat%2Fv2/account/relogin',
      '/v1/platforms/we%20chat%2Fv2/account/pin',
      '/v1/platforms/we%20chat%2Fv2/account/qrcode',
      '/v1/platforms/we%20chat%2Fv2/account/scope',
      '/v1/platforms/we%20chat%2Fv2/account/scope/migrate',
    ]);
  });

  it('names the switch target with the platform-neutral account field', async () => {
    const fetchMock = stubAccountReplies();

    await switchPlatformAccount('secret-token', 'qq', '1000002');

    expect(fetchMock).toHaveBeenCalledWith('/v1/platforms/qq/account/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer secret-token' },
      body: '{"account":"1000002"}',
    });
  });

  it('posts an empty body for relogin and pin', async () => {
    const fetchMock = stubAccountReplies();

    await enterPlatformLoginMode('secret-token', 'qq');
    await pinPlatformAccount('secret-token', 'qq');

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ['/v1/platforms/qq/account/relogin', 'POST', '{}'],
      ['/v1/platforms/qq/account/pin', 'POST', '{}'],
    ]);
  });

  it('requests the login qrcode', async () => {
    const fetchMock = stubFetch({ qrcode: 'qr-payload', reachable: true });

    await expect(getPlatformLoginQrcode('secret-token', 'qq')).resolves.toEqual({
      qrcode: 'qr-payload', reachable: true,
    });
    expect(fetchMock).toHaveBeenCalledWith('/v1/platforms/qq/account/qrcode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer secret-token' },
      body: '{}',
    });
  });

  it('keeps an unreachable platform out of the qrcode result', async () => {
    stubFetch({ qrcode: '', reachable: false, reason: '容器还没起来' });

    await expect(getPlatformLoginQrcode('secret-token', 'qq')).resolves.toEqual({
      qrcode: '', reachable: false, reason: '容器还没起来',
    });
  });

  it('sends the picked migration source, an empty one included', async () => {
    const fetchMock = stubAccountReplies();

    await getAccountScope('secret-token', 'qq');
    await getAccountScope('secret-token', 'qq', '20002');
    await getAccountScope('secret-token', 'qq', '20002', '');

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/v1/platforms/qq/account/scope',
      '/v1/platforms/qq/account/scope?target=20002',
      '/v1/platforms/qq/account/scope?target=20002&source=',
    ]);
  });

  it('carries the same source rule into the migration body', async () => {
    const fetchMock = stubAccountReplies();

    await migrateAccountScope('secret-token', 'qq', '20002');
    await migrateAccountScope('secret-token', 'qq', '20002', '');

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ['/v1/platforms/qq/account/scope/migrate', 'POST', '{"target":"20002"}'],
      ['/v1/platforms/qq/account/scope/migrate', 'POST', '{"target":"20002","source":""}'],
    ]);
  });
});

describe('Instance provisioning client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('names the new instance by id and carries the one platform it runs', async () => {
    const plan = {
      id: 'xiaoyu', label: '小鱼', idx: 2, prefix: '/i/xiaoyu',
      ports: { napcat: 6103 }, workspace: '/srv/xiaoyu', platform: 'qq', account: '1000000002',
    };
    const fetchMock = stubFetch(plan);

    await expect(createInstance('secret-token', 'xiaoyu', '小鱼', 'qq', '1000000002')).resolves.toEqual(plan);
    expect(fetchMock).toHaveBeenCalledWith('/v1/instances', {
      method: 'POST',
      headers: { Authorization: 'Bearer secret-token', 'Content-Type': 'application/json' },
      body: '{"id":"xiaoyu","label":"小鱼","platform":"qq","account":"1000000002"}',
    });
  });

  it('sends an empty account when the new instance claims no account yet', async () => {
    const fetchMock = stubFetch(instancePlanReply('xiaoyu'));

    await createInstance('secret-token', 'xiaoyu', '', 'qq', '');

    expect(fetchMock.mock.calls.map(([, init]) => init?.body)).toEqual([
      '{"id":"xiaoyu","label":"","platform":"qq","account":""}',
    ]);
  });

  it('escapes the instance id in the progress and archive paths', async () => {
    const fetchMock = stubFetch(instanceProgressReply('a b/c'));

    await getInstanceProgress('secret-token', 'a b/c');
    await deleteInstance('secret-token', 'a b/c');

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
      ['/v1/instances/a%20b%2Fc/progress', undefined],
      ['/v1/instances/a%20b%2Fc', 'DELETE'],
    ]);
  });

  // 回执读不懂时工作区已经建好、交给 root 侧了：报成没建成，人会拿同一个名字再建一次。
  it('treats a creation whose receipt it cannot read as started', async () => {
    stubFetch({ ...instancePlanReply('xiaoyu'), ports: undefined });
    const failure = await createInstance('secret-token', 'xiaoyu', '', 'qq', '').catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ReceiptUnreadable);
    expect(failure).toMatchObject({ message: '实例 xiaoyu 已经开始建，可回执格式不对：缺 ports' });

    stubFetch({ ...instancePlanReply('xiaoyu'), ports: { napcat: '6103' } });
    await expect(createInstance('secret-token', 'xiaoyu', '', 'qq', ''))
      .rejects.toThrow('实例 xiaoyu 已经开始建，可回执格式不对：ports 不是 JSON 对象，或有一项不是数字');
  });

  it('reads a finished progress without the fields only a running one carries', async () => {
    const done = { id: 'xiaoyu', lines: ['DONE ok'], finished: true, ok: true, detail: '' };
    stubFetch(done);

    await expect(getInstanceProgress('secret-token', 'xiaoyu')).resolves.toEqual(done);
  });

  // 补成空的话，没跑完的会被当成跑完了没成功，或者一直等下去。
  it.each([
    [{ ...instanceProgressReply('xiaoyu'), finished: undefined }, '缺 finished'],
    [{ ...instanceProgressReply('xiaoyu'), lines: 'DONE ok' }, 'lines 不是字符串数组'],
    [{ ...instanceProgressReply('xiaoyu'), state: 'paused' }, 'state 不是 queued、running 之一'],
    [{ ...instanceProgressReply('xiaoyu'), action: 'rename' }, 'action 不是 create、remove 之一'],
  ])('a progress reply that does not match its shape is refused (%#)', async (body, problem) => {
    stubFetch(body);

    await expect(getInstanceProgress('secret-token', 'xiaoyu')).rejects.toThrow(`实例 xiaoyu 的进度格式不对：${problem}`);
  });
});

describe('Platform catalog client', () => {
  const qq = {
    id: 'qq',
    display_name: 'QQ · NapCat',
    short_name: 'QQ',
    id_shape: 'digits',
    channels: [{ name: 'qq_group', kind: 'group', hashed: true }],
    has_config: true,
    hooks: [],
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('keeps the whole catalog and says which one this instance runs', async () => {
    stubFetch({ platforms: [qq], current: 'qq' });

    await expect(getPlatforms('secret-token')).resolves.toEqual({ platforms: [qq], current: 'qq' });
  });

  it('an older backend that names no current platform reads as null', async () => {
    stubFetch({ platforms: [qq] });

    await expect(getPlatforms('secret-token')).resolves.toEqual({ platforms: [qq], current: null });
  });

  it('a current of null reads as no platform named', async () => {
    stubFetch({ platforms: [qq], current: null });

    expect((await getPlatforms('secret-token')).current).toBeNull();
  });

  it.each([[''], [123]])('a current that is not a platform id is refused (%j)', async current => {
    stubFetch({ platforms: [qq], current });

    await expect(getPlatforms('secret-token')).rejects.toThrow('平台清单格式不对：current 不是非空字符串，也不是 null');
  });

  // 读成空清单的话，侧栏会悄悄退回内置的那几个平台，没人知道清单其实没读懂。
  it('a body without a platform list is refused, not read as an empty catalog', async () => {
    stubFetch({ current: 'qq' });

    await expect(getPlatforms('secret-token')).rejects.toThrow('平台清单格式不对：缺 platforms');
  });

  it.each([
    [{ platforms: {} }, 'platforms 不是数组'],
    [{ platforms: [null] }, 'platforms[0] 不是 JSON 对象'],
    [{ platforms: [{ ...qq, id: '' }] }, 'platforms[0].id 不是非空字符串'],
    [{ platforms: [qq, { ...qq, hooks: undefined }] }, '缺 platforms[1].hooks'],
    [{ platforms: [{ ...qq, channels: [{ name: 'qq_group', kind: 'group' }] }] }, '缺 platforms[0].channels[0].hashed'],
    [{ platforms: [{ ...qq, entrypoints: 'bot.py' }] }, 'platforms[0].entrypoints 不是字符串数组'],
  ])('a catalog that does not match its shape says where (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(getPlatforms('secret-token')).rejects.toThrow(`平台清单格式不对：${problem}`);
  });
});

describe('Upstream model list client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('asks for the named channel and hands back its models', async () => {
    const fetchMock = stubFetch({ models: ['gpt-4o', 'gpt-4o-mini'] });

    await expect(listUpstreamModels('secret-token', 'relay b', { slot: 'vision' })).resolves.toEqual(['gpt-4o', 'gpt-4o-mini']);
    expect(fetchMock.mock.calls[0][0]).toBe('/v1/config/providers/relay%20b/models');
    expect(fetchMock.mock.calls[0][1]?.body).toBe('{"slot":"vision"}');
  });

  // 补成空表的话，拉取按钮会报「0 个模型」，像是这家真的一个模型都没有。
  it.each([
    [[], '返回的不是 JSON 对象'],
    [{}, '缺 models'],
    [{ models: 'gpt-4o' }, 'models 不是字符串数组'],
    [{ models: ['gpt-4o', { id: 'gpt-4o-mini' }] }, 'models 不是字符串数组'],
  ])('a reply that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(listUpstreamModels('secret-token', 'openai', {})).rejects.toThrow(`模型列表格式不对：${problem}`);
  });
});

describe('Instance list client', () => {
  const root = { id: 'main', label: '主实例', path: '', current: true, idx: 0, platform: 'qq', account: '1000001' };
  const other = { id: 'xiaoyu', label: '小鱼', path: '/i/xiaoyu', current: false, idx: 1, platform: 'qq', account: '' };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads every row with the admin token', async () => {
    const fetchMock = stubFetch({ current_prefix: '', instances: [root, other] });

    await expect(getInstances('secret-token')).resolves.toEqual([root, other]);
    expect(fetchMock.mock.calls[0][0]).toBe('/v1/instances');
  });

  it('reads a creation still in progress with its progress', async () => {
    const building = { ...other, provisioning: instanceProgressReply('xiaoyu') };
    stubFetch({ current_prefix: '', instances: [root, building] });

    await expect(getInstances('secret-token')).resolves.toEqual([root, building]);
  });

  it('a row from an older roster without index, platform and account still reads', async () => {
    const legacy = { id: 'main', label: '主实例', path: '', current: true };
    stubFetch({ instances: [legacy] });

    await expect(getInstances('secret-token')).resolves.toEqual([legacy]);
  });

  // 读成空清单就是「单实例」：别的实例登着的号不再被拦，同一个号会在两边同时登录。
  it.each([
    [{}, '缺 instances'],
    [{ instances: {} }, 'instances 不是数组'],
    [{ instances: [root, 'xiaoyu'] }, 'instances[1] 不是 JSON 对象'],
    [{ instances: [{ ...root, current: 'yes' }] }, 'instances[0].current 不是 true 或 false'],
    [{ instances: [root, { ...other, path: undefined }] }, '缺 instances[1].path'],
    [{ instances: [{ ...other, idx: '1' }] }, 'instances[0].idx 不是数字'],
    [{ instances: [{ ...other, provisioning: 'running' }] }, 'instances[0].provisioning 不是 JSON 对象'],
    [{ instances: [{ ...other, provisioning: { ...instanceProgressReply('xiaoyu'), lines: 'x' } }] }, 'instances[0].provisioning.lines 不是字符串数组'],
  ])('a reply that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(getInstances('secret-token')).rejects.toThrow(`实例列表格式不对：${problem}`);
  });

  // 后端读不懂 instances.yaml 时回 503 带原因：页面要照原因报错，不能当成单实例。
  it('a roster the backend cannot read comes back as an error carrying its reason', async () => {
    const reason = '实例清单 /srv/kazebot-data/instances.yaml 读不懂：第 3 行第 31 列：mapping values are not allowed here';
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: reason }), {
      status: 503, headers: { 'Content-Type': 'application/json' },
    })));

    const failure = await getInstances('secret-token').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ status: 503, detail: reason, message: `503 ${reason}` });
  });
});

describe('Memory console client', () => {
  const namespace = {
    key: 'conv_abc',
    namespace: 'conv_abc',
    kind: 'conversation',
    subject: '',
    entry_count: 2,
    books: ['main'],
    unreadable_books: ['broken'],
    updated_at: '2026-10-28T01:00:00+00:00',
    owner: { kind: 'group', label: '某群', conversation_key: 'qq_group:abc', name_available: true },
  };
  const memoryEntry = { id: 'm1', book: 'main', content: '记一笔', keywords: ['笔'], constant: false, enabled: true, scan_depth: 4 };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads every source with its owner', async () => {
    const fetchMock = stubFetch({ namespaces: [namespace] });

    await expect(getMemoryOverview('secret-token')).resolves.toEqual([namespace]);
    expect(fetchMock.mock.calls[0][0]).toBe('/v1/admin/config/memory/overview');
  });

  it('an older backend that does not name unreadable books reads as none named', async () => {
    const { unreadable_books: _omitted, ...older } = namespace;
    stubFetch({ namespaces: [older] });

    await expect(getMemoryOverview('secret-token')).resolves.toEqual([{ ...older, unreadable_books: [] }]);
  });

  // 补成空表的话，页面会说「还没有任何记忆」。
  it.each([
    [{}, '缺 namespaces'],
    [{ namespaces: {} }, 'namespaces 不是数组'],
    [{ namespaces: [{ ...namespace, key: '' }] }, 'namespaces[0].key 不是非空字符串'],
    [{ namespaces: [{ ...namespace, entry_count: '2' }] }, 'namespaces[0].entry_count 不是数字'],
    [{ namespaces: [{ ...namespace, books: 'main' }] }, 'namespaces[0].books 不是字符串数组'],
    [{ namespaces: [{ ...namespace, unreadable_books: 'broken' }] }, 'namespaces[0].unreadable_books 不是字符串数组'],
    [{ namespaces: [{ ...namespace, updated_at: undefined }] }, '缺 namespaces[0].updated_at'],
    [{ namespaces: [{ ...namespace, updated_at: 1730077200 }] }, 'namespaces[0].updated_at 不是字符串'],
    [{ namespaces: [{ ...namespace, owner: null }] }, 'namespaces[0].owner 不是 JSON 对象'],
    [{ namespaces: [namespace, { ...namespace, owner: { kind: 'group' } }] }, '缺 namespaces[1].owner.label'],
  ])('a source list that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(getMemoryOverview('secret-token')).rejects.toThrow(`记忆来源列表格式不对：${problem}`);
  });

  it('reads the entries of one source, hand-written fields left as written', async () => {
    const handWritten = { id: 42, book: 'old', content: '手写的', keywords: '单个词', constant: true, enabled: true, scan_depth: 'deep' };
    const unreadable = [{ book: 'broken', problem: '第 3 行第 5 列：mapping values are not allowed here' }];
    const fetchMock = stubFetch({ entries: [memoryEntry, handWritten], unreadable_books: unreadable });

    await expect(getMemoryEntries('secret-token', 'conv abc')).resolves.toEqual({
      entries: [memoryEntry, handWritten], unreadable_books: unreadable,
    });
    expect(fetchMock.mock.calls[0][0]).toBe('/v1/admin/config/memory/conv%20abc/entries');
  });

  it('an older backend without unreadable_books reads as none named', async () => {
    stubFetch({ entries: [memoryEntry] });

    await expect(getMemoryEntries('secret-token', 'conv_abc')).resolves.toEqual({ entries: [memoryEntry], unreadable_books: [] });
  });

  // 补成空表的话，页面会说这一份「0 条」。
  it.each([
    [{}, '缺 entries'],
    [{ entries: null }, 'entries 不是数组'],
    [{ entries: [memoryEntry, 'm2'] }, 'entries[1] 不是 JSON 对象'],
    [{ entries: [{ ...memoryEntry, book: '' }] }, 'entries[0].book 不是非空字符串'],
    [{ entries: [{ ...memoryEntry, constant: 'false' }] }, 'entries[0].constant 不是 true 或 false'],
    [{ entries: [], unreadable_books: 'broken' }, 'unreadable_books 不是数组'],
    [{ entries: [], unreadable_books: [{ book: '', problem: 'x' }] }, 'unreadable_books[0].book 不是非空字符串'],
    [{ entries: [], unreadable_books: [{ book: 'broken' }] }, '缺 unreadable_books[0].problem'],
  ])('an entry list that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(getMemoryEntries('secret-token', 'conv_abc')).rejects.toThrow(`记忆条目列表格式不对：${problem}`);
  });

  it('reads the saved entry and the cleared count from their receipts', async () => {
    const saved = { ...memoryEntry, source: 'manual', created_at: '2026-10-28T01:00:00+00:00', updated_at: '2026-10-28T01:00:00+00:00' };
    stubFetch({ ok: true, entry: saved });
    await expect(chat.saveMemoryEntry('secret-token', 'conv_abc', { id: 'm1', content: '记一笔' })).resolves.toEqual(saved);

    stubFetch({ ok: true, removed: 3 });
    await expect(chat.clearMemoryNamespace('secret-token', 'conv_abc')).resolves.toBe(3);
  });

  // 存上了、清空了，只是回执读不懂：报成失败的话，人会再做一次。
  it('treats a save or clear whose receipt it cannot read as done', async () => {
    stubFetch({ ok: true, entry: { ...memoryEntry, constant: undefined } });
    const save = await chat.saveMemoryEntry('secret-token', 'conv_abc', { id: 'm1' }).catch((error: unknown) => error);
    expect(save).toBeInstanceOf(ReceiptUnreadable);
    expect(save).toMatchObject({ message: '这条记忆已经存上，可回执格式不对：缺 entry.constant' });

    // 补成 0 的话页面会说「已清空 0 条」。
    stubFetch({ ok: true });
    const clear = await chat.clearMemoryNamespace('secret-token', 'conv_abc').catch((error: unknown) => error);
    expect(clear).toBeInstanceOf(ReceiptUnreadable);
    expect(clear).toMatchObject({ message: '这一份记忆已经清空，可回执格式不对：缺 removed' });
  });

  it('an explicit ok false in a save or clear reply reads as not done', async () => {
    stubFetch({ ok: false, entry: memoryEntry, removed: 0 });

    await expect(chat.saveMemoryEntry('secret-token', 'conv_abc', { id: 'm1' })).rejects.toThrow('服务端没有存上这条记忆，请重试');
    await expect(chat.clearMemoryNamespace('secret-token', 'conv_abc')).rejects.toThrow('服务端没有清空这一份记忆，请重试');
  });
});

describe('Conversation list client', () => {
  const row = {
    session_id: 'sess-1',
    conversation_key: 'qq_group:abc',
    owner: { kind: 'group', label: '某群' },
    current_account: true,
    channel: 'qq_group',
    bytes: 2048,
    updated_at: 1761613200.5,
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads every conversation, an orphan without owner included', async () => {
    const orphan = { ...row, session_id: 'sess-2', conversation_key: '', owner: null };
    stubFetch({ conversations: [row, orphan] });

    await expect(getConversations('secret-token')).resolves.toEqual([row, orphan]);
  });

  it('a row from an older backend without the account flag still reads', async () => {
    const { current_account: _dropped, ...legacy } = row;
    stubFetch({ conversations: [legacy] });

    await expect(getConversations('secret-token')).resolves.toEqual([legacy]);
  });

  // 补成空表的话，记忆页会说「还没有任何会话」，计划卡片找不到会话。
  it.each([
    [{}, '缺 conversations'],
    [{ conversations: [{ ...row, session_id: '' }] }, 'conversations[0].session_id 不是非空字符串'],
    [{ conversations: [{ ...row, bytes: '2K' }] }, 'conversations[0].bytes 不是数字'],
    [{ conversations: [{ ...row, owner: 'group' }] }, 'conversations[0].owner 不是 JSON 对象，也不是 null'],
    [{ conversations: [{ ...row, owner: { kind: 'group' } }] }, '缺 conversations[0].owner.label'],
    [{ conversations: [{ ...row, current_account: 'yes' }] }, 'conversations[0].current_account 不是 true 或 false'],
  ])('a conversation list that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(getConversations('secret-token')).rejects.toThrow(`会话列表格式不对：${problem}`);
  });

  it('reads the latest messages of one conversation, multimodal content included', async () => {
    const reply = {
      total: 3,
      messages: [
        { id: 'm1', role: 'user', content: [{ type: 'text', text: '看图' }, { type: 'image_url', image_url: { url: 'data/attachments/a.png' } }] },
        { id: 'm2', role: 'assistant', content: '是一只猫', tool_calls: [] },
      ],
    };
    const fetchMock = stubFetch(reply);

    await expect(chat.getConversationMessages('secret-token', 'sess 1', 2)).resolves.toEqual(reply);
    expect(fetchMock.mock.calls[0][0]).toBe('/v1/admin/conversations/sess%201/messages?limit=2');
  });

  // 补成空表的话，预览会说「共 0 条」。
  it.each([
    [{ messages: [] }, '缺 total'],
    [{ total: 1 }, '缺 messages'],
    [{ total: 1, messages: [{ content: 'hi' }] }, '缺 messages[0].role'],
    [{ total: 1, messages: [{ role: 'user', content: null }] }, 'messages[0].content 不是字符串，也不是对象数组'],
  ])('a message preview that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(chat.getConversationMessages('secret-token', 'sess-1')).rejects.toThrow(`会话记录格式不对：${problem}`);
  });

  it('treats a context deletion whose receipt it cannot read as done', async () => {
    stubFetch({ ok: true, old_session_id: 'sess-1', conversation_key: 'qq_group:abc', cleared_children: 0 });
    await expect(chat.resetConversationBySession('secret-token', 'sess-1')).resolves.toMatchObject({ ok: true });

    stubFetch({ ok: true });
    const failure = await chat.resetConversationBySession('secret-token', 'sess-1').catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ReceiptUnreadable);
    expect(failure).toMatchObject({ message: '会话上下文已经删掉，可回执格式不对：缺 old_session_id' });

    stubFetch({ ok: false, old_session_id: '', conversation_key: '', cleared_children: 0 });
    await expect(chat.resetConversationBySession('secret-token', 'sess-1')).rejects.toThrow('服务端没有重置会话，请重试');
  });
});

describe('Log file list client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads the log files', async () => {
    const files = [{ name: 'supervisor.log', size: 1024, modified: 1761613200.5 }];
    stubFetch({ files });

    await expect(listLogFiles('secret-token')).resolves.toEqual(files);
  });

  // 补成空表的话，运行页会说「还没有日志文件」。
  it.each([
    [{}, '缺 files'],
    [{ files: [{ name: '', size: 1, modified: 1 }] }, 'files[0].name 不是非空字符串'],
    [{ files: [{ name: 'a.log', size: '1K', modified: 1 }] }, 'files[0].size 不是数字'],
  ])('a log list that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(listLogFiles('secret-token')).rejects.toThrow(`日志列表格式不对：${problem}`);
  });
});

describe('Drawtools bundle client', () => {
  const settings = {
    content: 'api: {}\n', hash: 'settings-1', exists: true, using_example: false,
    path: 'config/drawtools/settings.yaml', example_path: 'tools/drawtools/settings.example.yaml',
  };
  const missing = { content: '', hash: '', exists: false, using_example: false, path: 'config/drawtools/prompts/novelai/tag-guide.md' };
  const bundle = {
    settings,
    character_tags: { ...settings, hash: 'characters-1', path: 'config/drawtools/character_tags.yaml' },
    prompts: { top_system: { ...settings, hash: 'top-1' }, output_format: { ...settings, hash: 'fmt-1' }, tag_guide: missing },
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads every file with its fingerprint, a file neither there nor shipped included', async () => {
    stubFetch(bundle);

    await expect(getDrawtoolsBundle('secret-token')).resolves.toEqual(bundle);
  });

  // 指纹拿 null 顶上的话，保存时不带基准，绘图配置被整份盖掉。
  it.each([
    [{ ...bundle, settings: { ...settings, hash: undefined } }, '缺 settings.hash'],
    [{ ...bundle, character_tags: { ...settings, content: null } }, 'character_tags.content 不是字符串'],
    [{ ...bundle, prompts: { ...bundle.prompts, tag_guide: { ...missing, exists: undefined } } }, '缺 prompts.tag_guide.exists'],
    [{ ...bundle, prompts: [] }, 'prompts 不是 JSON 对象'],
    [{ settings, character_tags: settings }, '缺 prompts'],
  ])('a bundle that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(getDrawtoolsBundle('secret-token')).rejects.toThrow(`绘图配置格式不对：${problem}`);
  });
});

describe('Session children client', () => {
  const child = {
    session_id: 'agent:coder:web:a',
    parent_session_id: 'web:a',
    route_parent_session_id: 'web:a',
    node_id: 'coder',
    context_key: '',
    context_mode: 'fresh',
    status: 'running',
    task_id: 'task-1',
    started_at: '2026-10-28T01:00:00+00:00',
    updated_at: '2026-10-28T01:00:05+00:00',
    completed_at: '',
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads the child rows of the session', async () => {
    const fetchMock = stubFetch([child]);

    await expect(getSessionChildren('web:a')).resolves.toEqual([child]);
    expect(fetchMock.mock.calls[0][0]).toBe('/v1/sessions/web%3Aa/children');
  });

  it('a row with only its session id still reads', async () => {
    stubFetch([{ session_id: 'agent:coder:web:a' }]);

    await expect(getSessionChildren('web:a')).resolves.toEqual([{ session_id: 'agent:coder:web:a' }]);
  });

  it.each([
    [{ children: [child] }, '返回的不是数组'],
    [[child, null], '[1] 不是 JSON 对象'],
    [[{ ...child, session_id: '' }], '[0].session_id 不是非空字符串'],
    [[{ ...child, status: 3 }], '[0].status 不是字符串'],
  ])('a reply that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(getSessionChildren('web:a')).rejects.toThrow(`子会话列表格式不对：${problem}`);
  });
});

describe('Session running tasks client', () => {
  const task = {
    task_id: 'task-1',
    node_id: 'bootstrap.entry',
    status: 'running',
    created_at: '2026-10-28T01:00:00+00:00',
    caller_task_id: '',
    is_user_entry: true,
    source_inbound_seq: 3,
    branch_session_id: 'branch_1',
    parent_session_id: 'web:a',
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads the queued, running and waiting tasks of the session', async () => {
    const waiting = { ...task, task_id: 'task-3', status: 'suspended' };
    const fetchMock = stubFetch({ tasks: [task, { ...task, task_id: 'task-2', source_inbound_seq: null }, waiting] });

    await expect(getSessionRunningTasks('web:a')).resolves.toEqual([task, { ...task, task_id: 'task-2', source_inbound_seq: null }, waiting]);
    // 挂起等子 agent 的也要列上，不然正在等子 agent 的会话重连后会被标成空闲
    expect(fetchMock.mock.calls[0][0]).toBe('/v1/sessions/web%3Aa/running_tasks?include_suspended=true');
  });

  // 读成空表就是「什么都没在跑」，会话的生成中标记会被错清掉；抛出来的话调用方按「不知道」处理。
  it.each([
    [{}, '缺 tasks'],
    [{ tasks: null }, 'tasks 不是数组'],
    [{ tasks: [{ ...task, is_user_entry: 'true' }] }, 'tasks[0].is_user_entry 不是 true 或 false'],
    [{ tasks: [task, { ...task, branch_session_id: undefined }] }, '缺 tasks[1].branch_session_id'],
    [{ tasks: [{ ...task, source_inbound_seq: '3' }] }, 'tasks[0].source_inbound_seq 不是数字，也不是 null'],
  ])('a reply that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(getSessionRunningTasks('web:a')).rejects.toThrow(`运行中的任务列表格式不对：${problem}`);
  });
});

describe('Provider profiles client', () => {
  const body = {
    options: { openai: [{ key: 'temperature', type: 'float' }] },
    wire_formats: { openai: 'openai', 'gemini-relay': 'gemini' },
    host_profiles: { 'api.openai.com': ['openai'] },
    default_vision: { openai: true },
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads every table under its client name', async () => {
    stubFetch(body);

    await expect(getProviderProfiles('secret-token')).resolves.toEqual({
      options: body.options,
      wireFormats: body.wire_formats,
      hostProfiles: body.host_profiles,
      defaultVision: body.default_vision,
    });
  });

  it('an older backend without some tables reads them as empty', async () => {
    stubFetch({ options: body.options });

    await expect(getProviderProfiles('secret-token')).resolves.toEqual({
      options: body.options, wireFormats: {}, hostProfiles: {}, defaultVision: {},
    });
  });

  it.each([[[]], ['ok'], [null], [42]] as const)('a body that is not an object is refused (%j)', async received => {
    stubFetch(received);

    await expect(getProviderProfiles('secret-token')).rejects.toThrow('渠道参数清单格式不对：返回的不是 JSON 对象');
  });

  // 补成空表的话，界面会当这几家什么参数都不支持、哪个域名都认不出。
  it.each([
    ['options', []],
    ['wire_formats', 'openai'],
    ['host_profiles', null],
    ['default_vision', 1],
  ] as const)('a %s that is there but not an object is refused, not read as empty', async (key, value) => {
    stubFetch({ ...body, [key]: value });

    await expect(getProviderProfiles('secret-token')).rejects.toThrow(`渠道参数清单格式不对：${key} 不是 JSON 对象`);
  });
});

describe('Node and provider table client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads the node table and refuses one that is not a list of nodes', async () => {
    stubFetch([{ id: 'main', type: 'ai', model: '' }]);
    await expect(getNodes('secret-token')).resolves.toEqual([{ id: 'main', type: 'ai', model: '' }]);

    stubFetch({ detail: 'not a list' });
    await expect(getNodes('secret-token')).rejects.toThrow('节点表格式不对：返回的不是数组');
    stubFetch([{ type: 'ai' }]);
    await expect(getNodes('secret-token')).rejects.toThrow('节点表格式不对：缺 [0].id');
    stubFetch([{ id: 'main', type: 'ai', model: null }]);
    await expect(getNodes('secret-token')).rejects.toThrow('节点表格式不对：[0].model 不是字符串');
  });

  it('refuses a provider table with a missing or mistyped part instead of filling it with nothing', async () => {
    const complete = providerTable({
      providers: { openai: providerBlock({ model: 'gpt-5.4', model_raw: 'gpt-5.4' }) },
      fallbacks: [fallbackEntry({ provider: 'deepseek' })],
      node_fallbacks: { main: [fallbackEntry()] },
      registered: ['openai', 'deepseek'],
    });
    stubFetch(complete);
    await expect(getProviders('secret-token')).resolves.toEqual(complete);

    const { registered: _registered, ...unregistered } = complete;
    stubFetch(unregistered);
    await expect(getProviders('secret-token')).rejects.toThrow('渠道表格式不对：缺 registered');
    const { model_raw: _modelRaw, ...partial } = complete.providers.openai;
    stubFetch({ ...complete, providers: { openai: partial } });
    await expect(getProviders('secret-token')).rejects.toThrow('渠道表格式不对：缺 providers.openai.model_raw');
    stubFetch({ ...complete, fallbacks: { deepseek: {} } });
    await expect(getProviders('secret-token')).rejects.toThrow('渠道表格式不对：fallbacks 不是数组');
    const { options: _options, ...bare } = fallbackEntry();
    stubFetch({ ...complete, node_fallbacks: { main: [bare] } });
    await expect(getProviders('secret-token')).rejects.toThrow('渠道表格式不对：缺 node_fallbacks.main[0].options');
  });

  it('treats a provider write whose receipt it cannot read as done, saying what was done first', async () => {
    stubFetch({ ok: true });
    const writes: Array<[Promise<unknown>, string]> = [
      [upsertProvider('secret-token', 'openai', { model: 'gpt-5.5' }), '渠道 openai 已经存上，可回执'],
      [deleteProvider('secret-token', 'openai'), '渠道 openai 已经删掉，可回执'],
      [setActiveProvider('secret-token', 'openai'), '已经切到渠道 openai，可回执'],
      [updateFallbacks('secret-token', []), '全局备选链已经存上，可回执'],
      [updateNodeFallbacks('secret-token', 'main', []), '节点 main 的备选链已经存上，可回执'],
      [clearNodeFallbacks('secret-token', 'main'), '节点 main 已经改回跟随全局链，可回执'],
    ];
    for (const [write, receipt] of writes) {
      await expect(write).rejects.toBeInstanceOf(ReceiptUnreadable);
      await expect(write).rejects.toThrow(`${receipt}格式不对：缺 active_provider`);
    }
  });
});

describe('Chat endpoint shapes', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const stubText = (text: string) => vi.stubGlobal('fetch', vi.fn(async () => new Response(text, {
    status: 200, headers: { 'Content-Type': 'text/html' },
  })));

  // 代理把登录页、报错页原样回成 200 时也是这一句：SyntaxError 的英文原话说不出是哪一份没读懂。
  it('a 2xx body that is not JSON reads as a format problem of that reply', async () => {
    stubText('<html>login</html>');
    await expect(chat.getActiveNode('sess-1')).rejects.toThrow('会话的当前节点格式不对：返回的不是 JSON');
    stubText('');
    await expect(chat.listSessions()).rejects.toThrow('会话列表格式不对：返回的不是 JSON');
  });

  it('reads sessions, history and the active node that match their shapes', async () => {
    const session = { session_id: 'sess-1', conversation_key: 'web:a', channel: 'web', created_at: '', updated_at: '' };
    stubFetch([session]);
    await expect(chat.listSessions()).resolves.toEqual([session]);

    const rows = [
      { id: 'u1', cursor: 0, role: 'user', content: [{ type: 'text', text: 'hi' }] },
      { id: 'a1', cursor: 1, role: 'assistant', content: '', tool_calls: [{ id: 'c1', name: 'finish', arguments: {} }] },
    ];
    stubFetch(rows);
    await expect(chat.getSessionHistory('sess-1')).resolves.toEqual(rows);

    stubFetch({ node_id: 'main', is_override: false, default_node_id: 'main' });
    await expect(chat.getActiveNode('sess-1')).resolves.toEqual({ node_id: 'main', is_override: false, default_node_id: 'main' });
  });

  // 会话列表读成空就是「一个对话都没有」；历史读成空，已有的消息会被当成没落盘清掉。
  it.each([
    [() => chat.listSessions(), {}, '会话列表格式不对：返回的不是数组'],
    [() => chat.listSessions(), [{ conversation_key: 'web:a' }], '会话列表格式不对：缺 [0].session_id'],
    [() => chat.listSessions(), [{ session_id: 's', conversation_key: 'web:a', channel: 'web', created_at: 0, updated_at: '' }], '会话列表格式不对：[0].created_at 不是字符串'],
    [() => chat.getSessionHistory('s'), [{ id: 'u1', role: 'user', content: 42 }], '历史消息格式不对：[0].content 不是字符串，也不是对象数组'],
    [() => chat.getSessionHistory('s'), [{ id: 'u1', content: 'hi' }], '历史消息格式不对：缺 [0].role'],
    [() => chat.getSessionHistory('s'), [{ id: 'u1', role: 'user', content: '', cursor: '3' }], '历史消息格式不对：[0].cursor 不是数字'],
    [() => chat.getSessionHistory('s'), [{ id: 'a1', role: 'assistant', content: '', tool_calls: [null] }], '历史消息格式不对：[0].tool_calls 不是数组，或有一项不是 JSON 对象'],
    [() => chat.getSessionMessages('s'), [{ role: 'user' }], '会话消息格式不对：缺 [0].content'],
    [() => chat.getActiveNode('s'), { node_id: 'main', is_override: 'no', default_node_id: 'main' }, '会话的当前节点格式不对：is_override 不是 true 或 false'],
    [() => chat.getConfig(), { provider: 'openai', openai: { base_url: '' }, default_entry_node_id: 'main' }, '全局配置格式不对：缺 openai.model'],
    [() => chat.getConfig(), { provider: 'openai', openai: { model: '', base_url: '' } }, '全局配置格式不对：缺 default_entry_node_id'],
    [() => chat.getSessionProviderOverride('s', 'secret-token'), [], '会话的渠道覆盖格式不对：返回的不是 JSON 对象'],
  ])('a reply that does not match its shape is refused (%#)', async (read, body, problem) => {
    stubFetch(body);

    await expect(read()).rejects.toThrow(problem);
  });

  it('treats a chat write whose receipt it cannot read as done, saying what was done first', async () => {
    stubFetch({ unexpected: true });
    const writes: Array<[() => Promise<unknown>, string]> = [
      [() => chat.uploadAttachment(new File(['x'], 'a.png'), 'web:a'), '附件 a.png 已经传上，可回执格式不对：缺 path'],
      [() => postInbound({ conversation_key: 'web:a', text: 'hi' }), '消息已经发出，可回执格式不对：缺 session_id'],
      [() => decideApproval('secret-token', 'ap-1', 'allow'), '审批决定已经交上，可回执格式不对：缺 approval_id'],
      [() => chat.cancelActiveTasks('sess-1'), '取消请求已经交上，可回执格式不对：缺 ok'],
      [() => chat.resetConversation('web:a'), '会话已经重置，可回执格式不对：缺 ok'],
      [() => chat.switchNode('sess-1', 'main'), '节点已经切换，可回执格式不对：缺 ok'],
      [() => chat.deleteSession('sess-1'), '会话已经删掉，可回执格式不对：缺 ok'],
      [() => chat.updateModelConfig('secret-token', { model: 'gpt-5.5' }), '模型配置已经存上，可回执格式不对：缺 provider'],
    ];
    for (const [write, problem] of writes) {
      const failure = await write().catch((error: unknown) => error);
      expect(failure).toBeInstanceOf(ReceiptUnreadable);
      expect(failure).toMatchObject({ message: problem });
    }

    stubText('ok');
    await expect(chat.updateSessionProviderOverride('sess-1', 'secret-token', {})).rejects.toThrow('会话的渠道覆盖已经存上，可回执不是 JSON');
    await expect(chat.clearSessionProviderOverride('sess-1', 'secret-token')).rejects.toThrow('会话的渠道覆盖已经清掉，可回执不是 JSON');
  });

  // 一个已经定案的审批回执里没有状态，就分不出生效的是不是这一次。
  it('an approval receipt without its status cannot be read', async () => {
    stubFetch({ approval_id: 'ap-1', operation: 'read_file' });

    await expect(decideApproval('secret-token', 'ap-1', 'allow')).rejects.toThrow('审批决定已经交上，可回执格式不对：缺 status');
  });

  it('a 2xx that says ok false is a refusal, not an unreadable receipt', async () => {
    stubFetch({ ok: false });

    await expect(chat.cancelActiveTasks('sess-1')).rejects.toThrow('服务端没有取消任务，请重试');
    await expect(chat.deleteSession('sess-1')).rejects.toThrow('服务端未确认删除会话，请重试');
    stubFetch({ ok: false, target_node_id: '', is_override: false, default_node_id: 'main' });
    await expect(chat.switchNode('sess-1', 'main')).rejects.toThrow('服务端没有切换节点，请重试');
  });

  it('puts the session id into the provider override path as one segment', async () => {
    const fetchMock = stubFetch({});

    await chat.getSessionProviderOverride('a/b', 'secret-token');

    expect(fetchMock.mock.calls[0][0]).toBe('/v1/sessions/a%2Fb/provider_override');
  });
});

describe('System endpoint shapes', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const task = {
    task_id: 'task-1', session_id: 'sess-1', node_id: null, status: 'running', kind: 'node',
    created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:01Z', worker_id: null, caller_task_id: null,
    input_summary: '', cancel_requested: false, current_phase: '', current_detail: '', lane: 'interactive', awaiting_approval: false,
  };
  const worker = {
    alive: true, pid: 9001, uptime_sec: 12, failures: 0, respawns: 0, given_up: false,
    last_exit_code: null, last_log: '', retry_in_sec: 0, generation: 'gen-1',
  };
  const runtime = { supervised: true, workers: { 'engine-1': worker }, tasks: { queued: 0, running: 1 }, started_at: '2026-06-01T00:00:00Z', uptime_sec: 12 };

  it('reads health, admin state, active tasks, runtime status and a log tail that match their shapes', async () => {
    stubFetch({ status: 'ok', uptime_seconds: 3 });
    await expect(checkHealth()).resolves.toEqual({ status: 'ok', uptime_seconds: 3 });

    const state = adminState({ pending_approvals: [pendingApproval('ap-1')] });
    stubFetch(state);
    await expect(getAdminState('secret-token')).resolves.toEqual(state);

    stubFetch([task, { ...task, task_id: 'task-2', lane: undefined }]);
    await expect(fetchActiveTasks('secret-token')).resolves.toHaveLength(2);

    stubFetch(runtime);
    await expect(chat.getRuntimeStatus('secret-token')).resolves.toEqual(runtime);

    stubFetch({ name: 'engine.log', text: 'ready', truncated: false });
    await expect(chat.readLogTail('secret-token', 'engine.log')).resolves.toMatchObject({ text: 'ready', truncated: false });
  });

  // 计数补成 0、列表补成空：页面会说「没有待审批」「没有在跑的任务」「一个 worker 都没起来」。
  it.each([
    [() => checkHealth(), {}, '健康检查格式不对：缺 status'],
    [() => checkHealth(), { status: 'ok', uptime_seconds: '12' }, '健康检查格式不对：uptime_seconds 不是数字'],
    [() => getAdminState('t'), adminState({ sessions: undefined } as never), '管理状态格式不对：缺 sessions'],
    [() => getAdminState('t'), adminState({ approvals: { pending: '1' } } as never), '管理状态格式不对：approvals 不是 JSON 对象，或有一项不是数字'],
    [() => getAdminState('t'), adminState({ pending_approvals: [{ operation: 'x' }] } as never), '管理状态格式不对：缺 pending_approvals[0].approval_id'],
    [() => fetchActiveTasks('t'), { tasks: [] }, '活跃任务列表格式不对：返回的不是数组'],
    [() => fetchActiveTasks('t'), [{ ...task, status: 'completed' }], '活跃任务列表格式不对：[0].status 不是 running、pending、suspended 之一'],
    [() => fetchActiveTasks('t'), [{ ...task, awaiting_approval: undefined }], '活跃任务列表格式不对：缺 [0].awaiting_approval'],
    [() => chat.getRuntimeStatus('t'), { ...runtime, workers: [] }, '运行状态格式不对：workers 不是 JSON 对象'],
    [() => chat.getRuntimeStatus('t'), { ...runtime, workers: { 'engine-1': { ...worker, pid: '9001' } } }, '运行状态格式不对：workers.engine-1.pid 不是数字，也不是 null'],
    [() => chat.getRuntimeStatus('t'), { ...runtime, tasks: { queued: 0 } }, '运行状态格式不对：缺 tasks.running'],
    [() => chat.readLogTail('t', 'engine.log'), { text: 'ready' }, '日志 engine.log 的内容格式不对：缺 truncated'],
  ])('a reply that does not match its shape is refused (%#)', async (read, body, problem) => {
    stubFetch(body);

    await expect(read()).rejects.toThrow(problem);
  });

  it('treats a restart or config reload whose receipt it cannot read as done', async () => {
    stubFetch({ target: 'engine' });
    const restart = await chat.restartEngine('secret-token').catch((error: unknown) => error);
    expect(restart).toBeInstanceOf(ReceiptUnreadable);
    expect(restart).toMatchObject({ message: '重启请求已经交上，可回执格式不对：缺 scheduled' });

    stubFetch(restartReply({ scheduled: false, in_progress: { target: 'all', since: 12 } } as never));
    await expect(chat.restartEngine('secret-token')).rejects.toThrow('重启请求已经交上，可回执格式不对：in_progress.since 不是字符串');

    stubFetch({ ok: true });
    await expect(chat.reloadConfig('secret-token')).rejects.toThrow('配置已经重新读取，可回执格式不对：缺 config');
    stubFetch(configReloadReply());
    await expect(chat.reloadConfig('secret-token')).resolves.toMatchObject({ ok: true });
    stubFetch(restartReply());
    await expect(chat.restartEngine('secret-token')).resolves.toEqual(restartReply());
  });
});

describe('System model slot shapes', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const slot = {
    key: 'compact', label: '上下文压缩', desc: '', supports_provider: true, env_prefix: 'CLONOTH_COMPACT',
    model: '', base_url: '', model_raw: '', base_url_raw: '', provider: '', api_key_present: false, api_key_redacted: '',
  };
  const reply = { slots: [slot], image_tools: [{ name: 'gemini_image', slot: 'image_gemini', available: true }], image_default_channel: '' };

  it('reads the slots and hands back the full state after a write', async () => {
    stubFetch(reply);
    await expect(chat.getSystemModels('secret-token')).resolves.toEqual(reply);
    await expect(chat.updateSystemModel('secret-token', 'compact', { model: 'm' })).resolves.toEqual(reply);
    await expect(chat.updateImageDefaultChannel('secret-token', '')).resolves.toEqual(reply);
  });

  // model_raw 补成空串的话，编辑器回填的就是空的，保存一次就把变量引用抹掉了。
  it.each([
    [{ image_tools: [] }, '缺 slots'],
    [{ slots: [{ ...slot, model_raw: undefined }] }, '缺 slots[0].model_raw'],
    [{ slots: [{ ...slot, supports_provider: 'yes' }] }, 'slots[0].supports_provider 不是 true 或 false'],
    [{ slots: [slot], image_tools: [{ name: 'gpt_image', slot: 'image_gpt' }] }, '缺 image_tools[0].available'],
    [{ slots: [slot], image_default_channel: null }, 'image_default_channel 不是字符串'],
  ])('a slot list that does not match its shape is refused (%j)', async (body, problem) => {
    stubFetch(body);

    await expect(chat.getSystemModels('secret-token')).rejects.toThrow(`系统槽位格式不对：${problem}`);
  });

  it('treats a slot write whose receipt it cannot read as done', async () => {
    stubFetch({ ok: true });
    await expect(chat.updateSystemModel('secret-token', 'compact', { model: 'm' }))
      .rejects.toThrow('系统槽位 compact 已经存上，可回执格式不对：缺 slots');
    await expect(chat.updateImageDefaultChannel('secret-token', 'gemini_image'))
      .rejects.toBeInstanceOf(ReceiptUnreadable);
  });
});

describe('Sticker library client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const sticker = {
    sha256: 'abc', name: '猫', state: 'library', source: 'upload', tags: ['猫'], auto_tags: ['猫'], manual_tags: [],
    manual_override: false, caption_state: 'done', caption_error: '', width: 120, height: 120, animated: false, size: 2048,
    from_group: '', from_user: '', sent_count: 0, last_sent_at: 0, created_at: 1761613200, usable: true,
  };
  const counts = { pending: 1, library: 2, discarded: 0, usable: 1, awaiting_caption: 1 };

  it('reads a page of stickers with the counts of every state', async () => {
    const fetchMock = stubFetch({ items: [sticker], counts });

    await expect(chat.listStickers('secret-token', { state: 'library' })).resolves.toEqual({ items: [sticker], counts });
    expect(fetchMock.mock.calls[0][0]).toBe('/v1/stickers?state=library&search=&limit=60&offset=0');
  });

  // 补成空表、0 张的话，页面会说「这一档还没有图」「在库 0 张」。
  it.each([
    [{ counts }, '缺 items'],
    [{ items: [sticker] }, '缺 counts'],
    [{ items: [sticker], counts: { ...counts, usable: undefined } }, '缺 counts.usable'],
    [{ items: [{ ...sticker, state: 'archived' }], counts }, 'items[0].state 不是 pending、library、discarded 之一'],
    [{ items: [{ ...sticker, caption_state: 'queued' }], counts }, 'items[0].caption_state 不是 pending、running、done、failed 之一'],
    [{ items: [{ ...sticker, tags: '猫' }], counts }, 'items[0].tags 不是字符串数组'],
  ])('a sticker page that does not match its shape is refused (%#)', async (body, problem) => {
    stubFetch(body);

    await expect(chat.listStickers('secret-token')).rejects.toThrow(`图库格式不对：${problem}`);
  });

  it('a sticker deleted right after the change comes back as null, not as an unreadable receipt', async () => {
    stubFetch({});
    await expect(chat.updateSticker('secret-token', 'abc', { name: '猫' })).resolves.toBeNull();
    await expect(chat.acceptSticker('secret-token', 'abc')).resolves.toBeNull();

    stubFetch(sticker);
    await expect(chat.acceptSticker('secret-token', 'abc')).resolves.toEqual(sticker);
  });

  // 改动已经做完，只是回执读不懂：报成失败的话，人会再做一次。
  it('treats a sticker write whose receipt it cannot read as done', async () => {
    stubFetch({ ...sticker, usable: undefined });
    const update = await chat.updateSticker('secret-token', 'abc', { name: '猫' }).catch((error: unknown) => error);
    expect(update).toBeInstanceOf(ReceiptUnreadable);
    expect(update).toMatchObject({ message: '这张图的名字和标签已经存上，可回执格式不对：缺 usable' });

    stubFetch({ queued: '3' });
    await expect(chat.recaptionStickers('secret-token', false))
      .rejects.toThrow('已经重新排进打标队列，可回执格式不对：queued 不是数字');

    stubFetch({ id: 'abc' });
    await expect(chat.uploadSticker('secret-token', new File(['x'], 'cat.png')))
      .rejects.toThrow('这张图已经传上，可回执格式不对：缺 sha256');

    stubFetch({ scanned: 3, added: 1, known: 1 });
    await expect(chat.importStickers('secret-token', 'data/inbox', false))
      .rejects.toThrow('目录已经导入，可回执格式不对：缺 skipped');
  });
});

describe('Config page endpoint shapes', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const tool = {
    name: 'exa_search', source: 'external', editable: true, description: '语义搜索', input_schema: {}, has_spec: true,
    file: 'tools/exa_search.py', timeout_sec: 30, origin: 'bundled',
  };
  const skill = {
    name: '读网页', dir: 'jina-reader', path: 'config/skills/jina-reader/SKILL.md', description: '', enabled: true,
    strategy: 'normal', keywords: ['网页'], order: 0, priority: 0, scan_depth: 0, node_ids: [], body_preview: '',
  };
  const nodeFile = {
    name: 'qq.orchestrator.yaml', path: 'defaults/nodes/qq.orchestrator.yaml', kind: 'node', suffix: '.yaml', is_example: false,
    base_name: 'qq.orchestrator.yaml', size: 120, updated_at: 1761613200.5, source: 'default', same_as_default: false,
  };
  const effective = { name: 'read_file', registered: true, external: false, guarded: null, gated: '', automatic: '' };

  it('reads the tool, skill, node file and tool name lists that match their shapes', async () => {
    stubFetch([tool]);
    await expect(chat.getTools('secret-token')).resolves.toEqual([tool]);

    const broken = { name: 'broken', dir: 'broken', path: 'config/skills/broken/SKILL.md', problem: '第 2 行：frontmatter 写坏了' };
    stubFetch([skill, broken]);
    await expect(chat.getSkills('secret-token')).resolves.toEqual([skill, broken]);

    stubFetch([nodeFile]);
    await expect(chat.getNodeFiles('secret-token')).resolves.toEqual([nodeFile]);

    stubFetch(['read_file', 'exa_search']);
    await expect(getAllToolNames('secret-token')).resolves.toEqual(['read_file', 'exa_search']);

    const reply = { node_id: 'qq.orchestrator', mode: 'allowlist', dead_names: ['typo_tool'], tools: [effective] };
    stubFetch(reply);
    await expect(chat.getEffectiveTools('secret-token', 'qq.orchestrator')).resolves.toEqual(reply);
  });

  // 补成空表的话，页面会说「共 0 个」「暂无」，授权页一个「不可用」都不标。
  it.each([
    [() => chat.getTools('t'), { tools: [] }, '工具清单格式不对：返回的不是数组'],
    [() => chat.getTools('t'), [{ ...tool, source: 'mcp' }], '工具清单格式不对：[0].source 不是 builtin、plugin、external 之一'],
    [() => chat.getTools('t'), [{ ...tool, editable: undefined }], '工具清单格式不对：缺 [0].editable'],
    [() => chat.getSkills('t'), [{ ...skill, keywords: '网页' }], '技能列表格式不对：[0].keywords 不是字符串数组'],
    [() => chat.getSkills('t'), [{ ...skill, dir: '' }], '技能列表格式不对：[0].dir 不是非空字符串'],
    [() => chat.getNodeFiles('t'), [{ ...nodeFile, source: 'system' }], '节点文件列表格式不对：[0].source 不是 default、override、custom 之一'],
    [() => getAllToolNames('t'), ['read_file', 3], '工具名清单格式不对：返回的不是字符串数组'],
    [
      () => chat.getEffectiveTools('t', 'n'), { node_id: 'n', mode: 'all', dead_names: [], tools: [{ ...effective, guarded: 'no' }] },
      '节点 n 实际能用的工具格式不对：tools[0].guarded 不是 true 或 false，也不是 null',
    ],
    [() => chat.getEffectiveTools('t', 'n'), { node_id: 'n', mode: 'all', tools: [] }, '节点 n 实际能用的工具格式不对：缺 dead_names'],
    [() => chat.getMcpClients('t'), [{ transport: 'stdio' }], 'MCP Client 列表格式不对：缺 [0].id'],
  ])('a config list that does not match its shape is refused (%#)', async (read, body, problem) => {
    stubFetch(body);

    await expect(read()).rejects.toThrow(problem);
  });

  // 后端已照运行时的读法规整好；没规整过的开关报格式不对，不在前端替它纠正。
  it('reads the MCP client list as the backend normalizes it and refuses a raw switch', async () => {
    const alpha = { id: 'alpha', enabled: false, share_sessions: false, transport: 'stdio', command: 'run-me', args: ['--x'], env: {} };
    const beta = { id: 'beta', enabled: true, share_sessions: true, transport: 'streamable_http', url: 'http://x', headers: {} };
    stubFetch([alpha, beta]);

    await expect(chat.getMcpClients('secret-token')).resolves.toEqual([alpha, beta]);

    stubFetch([{ ...alpha, enabled: 'no' }]);
    await expect(chat.getMcpClients('secret-token')).rejects.toThrow('MCP Client 列表格式不对：[0].enabled 不是 true 或 false');
  });

  it('reads the receipts of a removal, a tool reload and a drawtools init', async () => {
    stubFetch({ ok: true, restored: true });
    await expect(chat.deleteNodeFile('secret-token', 'a.yaml')).resolves.toEqual({ ok: true, restored: true });

    stubFetch(toolsReloadReply(3));
    await expect(chat.reloadTools('secret-token')).resolves.toEqual(toolsReloadReply(3));

    stubFetch({ ok: true, created: ['config/drawtools/settings.yaml'] });
    await expect(chat.initDrawtoolsConfigs('secret-token')).resolves.toEqual({ ok: true, created: ['config/drawtools/settings.yaml'] });
  });

  // 删了、重载了、补上了、清理了，只是回执读不懂：报成失败的话，人会再做一次。
  it('treats a config write whose receipt it cannot read as done', async () => {
    stubFetch({ ok: true });
    const removed = await chat.deleteNode('secret-token', 'chat.intent').catch((error: unknown) => error);
    expect(removed).toBeInstanceOf(ReceiptUnreadable);
    expect(removed).toMatchObject({ message: '节点 chat.intent 本实例那份已经删掉，可回执格式不对：缺 restored' });
    await expect(chat.resetRuntime('secret-token')).rejects.toThrow('runtime.yaml 本实例那份已经删掉，可回执格式不对：缺 restored');
    await expect(chat.reloadTools('secret-token')).rejects.toThrow('工具重载已经交上，可回执格式不对：缺 run_id');
    await expect(chat.initDrawtoolsConfigs('secret-token')).rejects.toThrow('缺的绘图配置已经补上，可回执格式不对：缺 created');
    await expect(chat.cleanupDrawtoolsAttachments('secret-token'))
      .rejects.toThrow('绘图附件已经清理，可回执格式不对：缺 deleted_count');

    stubFetch({ deleted: true });
    await expect(chat.deleteSkill('secret-token', 'jina-reader')).rejects.toThrow('技能 jina-reader 已经删掉，可回执格式不对：缺 ok');
  });

  it('an explicit ok false in a config write reads as not done', async () => {
    stubFetch({ ok: false, restored: false });
    await expect(chat.deleteTool('secret-token', 'demo_tool')).rejects.toThrow('服务端没有删掉这份配置，请重试');

    stubFetch({ ...toolsReloadReply(), ok: false });
    await expect(chat.reloadTools('secret-token')).rejects.toThrow('服务端没有重载工具，请重试');
  });
});

describe('Hot config endpoint shapes', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads an unpublished and a published platform state as the supervisor sends them', async () => {
    stubFetch(unpublishedStateReply());
    await expect(chat.getPlatformState('secret-token', 'qq')).resolves.toEqual(unpublishedStateReply());

    stubFetch(publishedStateReply());
    await expect(chat.getPlatformState('secret-token', 'qq')).resolves.toEqual(publishedStateReply());
  });

  // 心跳时间没写或读不懂时后端给 null，这是一种正常的回答。
  it('a heartbeat age of null still reads', async () => {
    stubFetch(publishedStateReply(qqSnapshot(), { age_sec: null }));

    expect((await chat.getPlatformState('secret-token', 'qq')).age_sec).toBeNull();
  });

  // 补成空的话，来源会被说成「都跟着默认」，可写的层、生效值出自哪一版文件也一样。
  it.each([
    [{ ...unpublishedStateReply(), file: {} }, '缺 file.exists'],
    [publishedStateReply(qqSnapshot(), { age_sec: 'soon' as never }), 'age_sec 不是数字，也不是 null'],
    [{ ...publishedStateReply(), state: null }, 'state 不是 JSON 对象'],
    [
      publishedStateReply(qqSnapshot({ sources: { signal_at: 'elsewhere' as never } })),
      'state.sources 不是 JSON 对象，或有一项不是 platform、global、legacy、env、default 之一',
    ],
    [publishedStateReply(qqSnapshot({ layers: { signal_at: 'global' as never } })), 'state.layers 不是 JSON 对象，或有一项不是字符串数组'],
    [publishedStateReply(qqSnapshot({ loaded: { exists: true } as never })), '缺 state.loaded.mtime_ns'],
    [
      publishedStateReply(qqSnapshot({
        defaults: { config_path: 'features.yaml', file: { exists: false, mtime_ns: 0, size: 0 }, parse_error: '' } as never,
      })),
      '缺 state.defaults.loaded',
    ],
    [
      publishedStateReply(qqSnapshot({ capabilities: [{ ...qqSnapshot().capabilities![0], grants: 'admin' as never }] })),
      'state.capabilities[0].grants 不是字符串数组',
    ],
    [
      publishedStateReply(qqSnapshot({ input_rules: { allowed_groups: { kind: 'phone' as never } } })),
      'state.input_rules.allowed_groups.kind 不是 id、word 之一',
    ],
    [
      publishedStateReply(qqSnapshot({ runtime: { volatile: { cached_groups: '3' as never } } })),
      'state.runtime.volatile.cached_groups 不是数字',
    ],
  ])('a platform state that does not match its shape says where (%#)', async (body, problem) => {
    stubFetch(body);

    await expect(chat.getPlatformState('secret-token', 'qq')).rejects.toThrow(`平台 qq 的生效状态格式不对：${problem}`);
  });

  it('a state body that is not JSON says which one, not a SyntaxError', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>502</html>', { status: 200, headers: { 'Content-Type': 'text/html' } })));

    await expect(chat.getPlatformState('secret-token', 'qq')).rejects.toThrow('平台 qq 的生效状态格式不对：返回的不是 JSON');
  });

  it('reads the global state with its adoption, fallbacks and central switches', async () => {
    stubFetch(featuresStateReply());
    await expect(chat.getFeaturesState('secret-token')).resolves.toEqual(featuresStateReply());

    const { fallbacks: _fallbacks, central: _central, ...older } = featuresStateReply();
    stubFetch(older);
    const read = await chat.getFeaturesState('secret-token');
    expect(read).not.toHaveProperty('fallbacks');
    expect(read).not.toHaveProperty('central');
  });

  // 生效情况读成空表，页面会说「后端没给出 bot 的生效情况」；兜底读丢了，页面会把平台实际用着的值说成默认值。
  it.each([
    [{ ...featuresStateReply(), platforms: undefined }, '缺 platforms'],
    [featuresStateReply({ platforms: [{ id: 'qq', display_name: 'QQ · NapCat', published: true } as never] }), '缺 platforms[0].applied'],
    [
      featuresStateReply({
        fallbacks: { sticker_auto_tag: [{ ...featuresStateReply().fallbacks!.sticker_auto_tag[0], source: 'default' as never }] },
      }),
      'fallbacks.sticker_auto_tag[0].source 不是 legacy、env 之一',
    ],
    [
      { ...featuresStateReply(), fallbacks: { sticker_auto_tag: [{ platform: 'qq', display_name: 'QQ · NapCat', source: 'env', file: '' }] } },
      '缺 fallbacks.sticker_auto_tag[0].value',
    ],
    [
      featuresStateReply({ central: { sticker_auto_tag: { enabled: 'yes' as never, set_globally: false } } }),
      'central.sticker_auto_tag.enabled 不是 true 或 false',
    ],
  ])('a global state that does not match its shape says where (%#)', async (body, problem) => {
    stubFetch(body);

    await expect(chat.getFeaturesState('secret-token')).rejects.toThrow(`全局设置的生效状态格式不对：${problem}`);
  });

  // 快照由后端测试盯着和 console_schema 的声明逐项一致，读得通才算认得后端真正发的布局。
  it('reads every console schema the backend declares', async () => {
    stubFetch(schemaSnapshot.features);
    await expect(chat.getFeaturesSchema('secret-token')).resolves.toEqual(schemaSnapshot.features);

    for (const [platformId, schema] of Object.entries(schemaSnapshot.platforms)) {
      stubFetch(schema);
      await expect(chat.getPlatformSchema('secret-token', platformId)).resolves.toEqual(schema);
    }
  });

  const signalAt = { kind: 'bool', label: '被 @ 时回应', path: 'trigger.signals.at', layers: ['global', 'platform'], default: null };
  const schemaWith = (nodes: unknown[], fields: Record<string, unknown> = { signal_at: signalAt }) => ({
    version: 1,
    sections: [{ id: 'trigger', label: '触发时机', icon: 'timer', note: '', paths: ['trigger'], nodes }],
    fields,
  });

  // 认不出的节点照猜着画，页面会少一块或摆错控件。
  it.each([
    [{ ...schemaWith([]), version: '1' }, 'version 不是数字'],
    [{ ...schemaWith([]), fields: undefined }, '缺 fields'],
    [schemaWith([{ kind: 'tabs' }]), 'sections[0].nodes[0].kind 不是 block、grid、panel、loose、fixed、field、desc、note、custom 之一'],
    [schemaWith([{ kind: 'grid' }]), '缺 sections[0].nodes[0].children'],
    [schemaWith([{ kind: 'block', title: '触发条件', children: [{ kind: 'field' }] }]), '缺 sections[0].nodes[0].children[0].key'],
    [schemaWith([{ kind: 'block', title: '触发条件', frame: 'dotted' }]), 'sections[0].nodes[0].frame 不是 ruled、boxed 之一'],
    [schemaWith([{ kind: 'field', key: 'signal_at' }], { signal_at: { ...signalAt, default: undefined } }), '缺 fields.signal_at.default'],
    [
      schemaWith([], { mode: { ...signalAt, kind: 'choice', choices: [['group']] } }),
      'fields.mode.choices 不是数组，或有一项不是「值、名字」两个字符串',
    ],
  ])('a console schema that does not match its shape says where (%#)', async (body, problem) => {
    stubFetch(body);

    await expect(chat.getPlatformSchema('secret-token', 'qq')).rejects.toThrow(`平台 qq 的控制台布局格式不对：${problem}`);
  });

  it('reads an audition the way the platform judged it', async () => {
    stubFetch(dryRunReply());
    await expect(chat.dryRunPlatformTrigger('secret-token', 'qq', [{ text: '小风在吗' }])).resolves.toEqual(dryRunReply());

    const { presence: _presence, ...older } = dryRunReply();
    stubFetch(older);
    expect(await chat.dryRunPlatformTrigger('secret-token', 'qq', [{ text: '小风在吗' }])).not.toHaveProperty('presence');
  });

  // 缺一项就会把「会不会接话」「被谁拦下」念错。
  it.each([
    [{ ...dryRunReply(), results: undefined }, '缺 results'],
    [dryRunReply({ results: [{ ...dryRunReply().results[0], triggered: 'yes' as never }] }), 'results[0].triggered 不是 true 或 false'],
    [
      dryRunReply({ results: [{ ...dryRunReply().results[0], quiet: 'expired' as never }] }),
      'results[0].quiet 不是 空串、waiting、merged、due、cancelled 之一',
    ],
    [dryRunReply({ presence: { active: true } as never }), '缺 presence.window_sec'],
  ])('an audition that does not match its shape says where (%#)', async (body, problem) => {
    stubFetch(body);

    await expect(chat.dryRunPlatformTrigger('secret-token', 'qq', [{ text: '小风在吗' }])).rejects.toThrow(`试听结果格式不对：${problem}`);
  });
});

describe('Feature request client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('hands the JSON body of a read back for the page to check', async () => {
    const fetchMock = stubFetch({ items: [] });

    await expect(featureRequest('/v1/community/activities', { what: '活动列表', scope: 'qq_group:a' })).resolves.toEqual({ items: [] });
    expect(fetchMock.mock.calls[0][0]).toBe(`${MOUNT}/v1/community/activities?scope=qq_group%3Aa`);
  });

  it('says which read came back as something other than JSON', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>ok</html>', { status: 200, headers: { 'Content-Type': 'text/html' } })));

    await expect(featureRequest('/v1/community/activities', { what: '活动列表' })).rejects.toThrow('活动列表格式不对：返回的不是 JSON');
  });

  // 写已经做完：正文读不懂报成失败的话，人会再做一次。
  it('a write whose receipt is not JSON reads as done but unreadable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('done', { status: 200 })));

    const outcome = await featureRequest('/v1/reminders', { method: 'POST', receipt: '提醒已经建好，可回执', body: {} }).catch((error: unknown) => error);

    expect(outcome).toBeInstanceOf(ReceiptUnreadable);
    expect(outcome).toMatchObject({ message: '提醒已经建好，可回执不是 JSON' });
  });

  it('a receipt reader that does not say what was done gets the done half put in front', () => {
    expect(() => readReceipt('计划已经确认，可回执', {}, () => { throw new Error('执行计划响应格式无效'); }))
      .toThrow('计划已经确认，可回执读不懂：执行计划响应格式无效');
    expect(() => readReceipt('计划已经确认，可回执', {}, () => { throw new Error('计划已经确认，可回执格式不对：缺 id'); }))
      .toThrow(/^计划已经确认，可回执格式不对：缺 id$/);
  });
});
