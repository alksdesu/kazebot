// [2026-05-16] Updated: tests for real Supervisor API client signatures.
import { afterEach, describe, expect, it, vi } from 'vitest';

import { postInbound, connectGlobalWS, checkHealth, getAdminState, checkAdminAuth, createInstance, decideApproval, deleteInstance, getAllToolNames, fetchActiveTasks, cancelTask, enterPlatformLoginMode, getAccountScope, getInstanceProgress, getPlatformAccount, getPlatformLoginQrcode, getPlatforms, migrateAccountScope, pinPlatformAccount, switchPlatformAccount } from '../api/supervisorClient';

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

  it('cancels a single task with admin context headers', async () => {
    // [AutoC 2026-06-04] Why: the cancel endpoint is public today, but the System
    // modal should still send the configured admin context. How: verify both the
    // normal bearer header and the explicit X-Admin-Token header. Purpose: future
    // backend hardening can accept the same client call without changing the UI.
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(cancelTask('secret-token', 'task-abc')).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith('/v1/tasks/task-abc/cancel', {
      method: 'POST',
      headers: { Authorization: 'Bearer secret-token', 'X-Admin-Token': 'secret-token' },
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
  const qq = { id: 'qq', display_name: 'QQ · NapCat', short_name: 'QQ', channels: [], has_config: true };

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

  it.each([[''], [123], [null]])('a current that is not a platform id reads as null (%s)', async current => {
    stubFetch({ platforms: [qq], current });

    expect((await getPlatforms('secret-token')).current).toBeNull();
  });

  it('a body without a platform list reads as an empty catalog', async () => {
    stubFetch({ current: 'qq' });

    await expect(getPlatforms('secret-token')).resolves.toEqual({ platforms: [], current: 'qq' });
  });
});
