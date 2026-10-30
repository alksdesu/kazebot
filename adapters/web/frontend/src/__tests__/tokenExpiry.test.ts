// 带着当前登录的令牌被回 401：和事件流 4401 一样回登录页；登录页验令牌、带着别的令牌、公开请求被拒都不算。

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, request, send } from '../api/request';
import { useSettingsStore } from '../store/settingsStore';

const REFUSED = '服务端不认当前令牌（令牌无效或已过期），请重新登录。';

const refusal = (): Response => ({
  ok: false,
  status: 401,
  statusText: 'Unauthorized',
  headers: { get: () => 'application/json' },
  text: async () => '{"detail":"Unauthorized"}',
}) as unknown as Response;

beforeEach(() => {
  useSettingsStore.setState({ adminToken: 'current', isAuthenticated: true, loginNotice: '' });
  vi.stubGlobal('fetch', vi.fn(async () => refusal()));
});
afterEach(() => {
  vi.unstubAllGlobals();
  useSettingsStore.setState({ adminToken: null, isAuthenticated: false, loginNotice: '' });
});

describe('令牌被回 401', () => {
  it('带着当前登录的令牌被拒就回登录页，说明原因，令牌留着', async () => {
    await expect(request('/v1/admin/state')).rejects.toBeInstanceOf(ApiError);

    expect(useSettingsStore.getState()).toMatchObject({ isAuthenticated: false, loginNotice: REFUSED, adminToken: 'current' });
  });

  it('还没登录时验令牌被拒不算，由登录页自己报', async () => {
    useSettingsStore.setState({ isAuthenticated: false });

    const response = await send('/v1/admin/auth/check', { token: 'current' });

    expect(response.status).toBe(401);
    expect(useSettingsStore.getState().loginNotice).toBe('');
  });

  it('带着别的令牌被拒不收回当前登录', async () => {
    await send('/v1/admin/plugins/frontend', { token: 'older' });

    expect(useSettingsStore.getState()).toMatchObject({ isAuthenticated: true, loginNotice: '' });
  });

  it('不带令牌的公开请求被拒也不算', async () => {
    await send('/v1/health', { anonymous: true });

    expect(useSettingsStore.getState()).toMatchObject({ isAuthenticated: true, loginNotice: '' });
  });
});
