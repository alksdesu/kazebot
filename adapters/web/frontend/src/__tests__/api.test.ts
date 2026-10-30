// Supervisor API client: endpoint shapes, which requests carry the admin token, and how server errors read.
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, clearNodeFallbacks, deleteProvider, getNodes, getProviders, ReceiptUnreadable, setActiveProvider, updateFallbacks, updateNodeFallbacks, upsertProvider, postInbound, connectGlobalWS, checkHealth, getAdminState, checkAdminAuth, createInstance, decideApproval, deleteInstance, getAllToolNames, fetchActiveTasks, cancelTask, enterPlatformLoginMode, getAccountScope, getConversations, getDrawtoolsBundle, getInstanceDirectory, getInstanceProgress, getInstances, getMemoryEntries, getMemoryOverview, getPlatformAccount, getPlatformLoginQrcode, getPlatforms, getProviderProfiles, getSessionChildren, getSessionRunningTasks, listLogFiles, listUpstreamModels, migrateAccountScope, pinPlatformAccount, switchPlatformAccount } from '../api/supervisorClient';
import { MOUNT } from '../instances/paths';
import { useSettingsStore } from '../store/settingsStore';
import { fallbackEntry, providerBlock, providerTable } from './fixtures/providerTable';

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
    const fetchMock = stubFetch({ current_path: MOUNT, instances: [{ label: '本实例', path: MOUNT }] });

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

  it('reads the account of the addressed platform', async () => {
    const fetchMock = stubFetch({ configured: true, account: '1000001' });

    await expect(getPlatformAccount('secret-token', 'qq')).resolves.toEqual({ configured: true, account: '1000001' });
    expect(fetchMock).toHaveBeenCalledWith('/v1/platforms/qq/account', {
      headers: { Authorization: 'Bearer secret-token' },
      signal: expect.any(AbortSignal),
    });
  });

  it('reads an empty account response as unconfigured', async () => {
    stubFetch(null);

    await expect(getPlatformAccount('secret-token', 'qq')).resolves.toEqual({ configured: false });
  });

  it('escapes the platform id in every account path', async () => {
    const fetchMock = stubFetch();

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
    const fetchMock = stubFetch();

    await switchPlatformAccount('secret-token', 'qq', '1000002');

    expect(fetchMock).toHaveBeenCalledWith('/v1/platforms/qq/account/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer secret-token' },
      body: '{"account":"1000002"}',
    });
  });

  it('posts an empty body for relogin and pin', async () => {
    const fetchMock = stubFetch();

    await enterPlatformLoginMode('secret-token', 'qq');
    await pinPlatformAccount('secret-token', 'qq');

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ['/v1/platforms/qq/account/relogin', 'POST', '{}'],
      ['/v1/platforms/qq/account/pin', 'POST', '{}'],
    ]);
  });

  it('requests the login qrcode and fills in the missing fields', async () => {
    const fetchMock = stubFetch({ qrcode: 'qr-payload' });

    await expect(getPlatformLoginQrcode('secret-token', 'qq')).resolves.toEqual({
      qrcode: 'qr-payload', reachable: true, reason: '',
    });
    expect(fetchMock).toHaveBeenCalledWith('/v1/platforms/qq/account/qrcode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer secret-token' },
      body: '{}',
    });
  });

  it('keeps an unreachable platform out of the qrcode result', async () => {
    stubFetch({ reachable: false, reason: '容器还没起来' });

    await expect(getPlatformLoginQrcode('secret-token', 'qq')).resolves.toEqual({
      qrcode: '', reachable: false, reason: '容器还没起来',
    });
  });

  it('sends the picked migration source, an empty one included', async () => {
    const fetchMock = stubFetch();

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
    const fetchMock = stubFetch();

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
    const fetchMock = stubFetch();

    await createInstance('secret-token', 'xiaoyu', '', 'qq', '');

    expect(fetchMock.mock.calls.map(([, init]) => init?.body)).toEqual([
      '{"id":"xiaoyu","label":"","platform":"qq","account":""}',
    ]);
  });

  it('escapes the instance id in the progress and archive paths', async () => {
    const fetchMock = stubFetch();

    await getInstanceProgress('secret-token', 'a b/c');
    await deleteInstance('secret-token', 'a b/c');

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
      ['/v1/instances/a%20b%2Fc/progress', undefined],
      ['/v1/instances/a%20b%2Fc', 'DELETE'],
    ]);
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
