// 共享配置组的客户端：打错一条 URL 就会把别的组的配置换给本实例，所以每条路径、
// 方法和请求体都钉住；应答一律按最脏的形状归一，缺字段不能让面板整块白掉。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createShareGroup,
  deleteShareGroup,
  getShareGroups,
  isValidShareGroupName,
  joinShareGroup,
  leaveShareGroup,
  type ShareGroupOverview,
} from '../api/supervisorClient';

const TOKEN = 'admin-token';
const AUTH = { Authorization: `Bearer ${TOKEN}` };
const WRITE_HEADERS = { 'Content-Type': 'application/json', ...AUTH };

const EMPTY: ShareGroupOverview = { available: false, reason: '', current: null, groups: [], archives: [] };

let body: unknown;
let status: number;

const fetchMock = () => vi.mocked(fetch);

beforeEach(() => {
  body = {};
  status = 200;
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })));
});

afterEach(() => { vi.unstubAllGlobals(); });

describe('isValidShareGroupName', () => {
  it.each(['ab', 'alpha', 'a_1', 'main_persona', `a${'b'.repeat(31)}`])('收下 %s', name => {
    expect(isValidShareGroupName(name)).toBe(true);
  });

  it.each([
    ['a', '单字符'],
    ['Alpha', '大写'],
    ['a-b', '连字符'],
    ['1alpha', '数字开头'],
    ['_alpha', '下划线开头'],
    ['alpha ', '尾随空格'],
    [`a${'b'.repeat(32)}`, '33 字符'],
    ['', '空名'],
  ])('挡掉 %s（%s）', name => {
    expect(isValidShareGroupName(name)).toBe(false);
  });
});

describe('读共享配置组全貌', () => {
  it('GET /share-groups，带令牌和读请求时限', async () => {
    body = { available: true, reason: '', current: null, groups: [], archives: [] };

    await expect(getShareGroups(TOKEN)).resolves.toEqual({
      available: true, reason: '', current: null, groups: [], archives: [],
    });
    expect(fetchMock()).toHaveBeenCalledExactlyOnceWith('/v1/share-groups', {
      headers: AUTH,
      signal: expect.any(AbortSignal),
    });
  });

  it('字段缺一整片时给出一份空全貌，而不是让页面读 undefined', async () => {
    body = {};
    await expect(getShareGroups(TOKEN)).resolves.toEqual(EMPTY);
  });

  it('把各种脏形状归一：非串的理由丢掉、空的当前组算没进组、成员缺备注退回 id', async () => {
    body = {
      available: 1,
      reason: 42,
      current: '',
      groups: [
        {
          name: 'alpha',
          members: [{ id: 'main' }, { id: 'xiaoyu', label: '第二实例', current: 1 }],
          created_at: '2026-01-02T03:04:05Z',
          created_by: 'main',
        },
        null,
        { name: 'beta', members: 'nope', created_at: 7 },
      ],
      archives: ['data/config.archived-1', 7],
    };

    await expect(getShareGroups(TOKEN)).resolves.toEqual({
      available: true,
      reason: '',
      current: null,
      groups: [
        {
          name: 'alpha',
          members: [
            { id: 'main', label: 'main', current: false },
            { id: 'xiaoyu', label: '第二实例', current: true },
          ],
          created_at: '2026-01-02T03:04:05Z',
          created_by: 'main',
        },
        { name: '', members: [], created_at: '', created_by: '' },
        { name: 'beta', members: [], created_at: '', created_by: '' },
      ],
      archives: ['data/config.archived-1', '7'],
    });
  });

  it('后端拒绝时按状态码抛出来，页面才好把原因摆出去', async () => {
    status = 403;
    body = { detail: '这台机器没开共享配置' };

    await expect(getShareGroups(TOKEN)).rejects.toThrow('403 这台机器没开共享配置');
  });
});

describe('建组、加入、退出、删组', () => {
  const change = {
    group: 'alpha',
    archived: 'data/config.archived-20260102',
    copied_env: ['OPENAI_API_KEY'],
    overview: { available: true, reason: '', current: 'alpha', groups: [], archives: [] },
  };

  it('建组 POST /share-groups，组名进请求体', async () => {
    body = change;

    await expect(createShareGroup(TOKEN, 'alpha')).resolves.toEqual(change);
    expect(fetchMock()).toHaveBeenCalledExactlyOnceWith('/v1/share-groups', {
      method: 'POST',
      headers: WRITE_HEADERS,
      body: JSON.stringify({ name: 'alpha' }),
    });
  });

  it('加入 POST /share-groups/<组名>/join，组名进路径且转义', async () => {
    body = change;

    await joinShareGroup(TOKEN, 'a b');
    expect(fetchMock()).toHaveBeenCalledExactlyOnceWith('/v1/share-groups/a%20b/join', {
      method: 'POST',
      headers: WRITE_HEADERS,
      body: '{}',
    });
  });

  it('退出 POST /share-groups/leave，不带组名 —— 后端只认本实例现在在哪个组', async () => {
    body = { ...change, overview: { ...change.overview, current: null } };

    await leaveShareGroup(TOKEN);
    expect(fetchMock()).toHaveBeenCalledExactlyOnceWith('/v1/share-groups/leave', {
      method: 'POST',
      headers: WRITE_HEADERS,
      body: '{}',
    });
  });

  it('删组 DELETE /share-groups/<组名>，不带请求体', async () => {
    body = { ...change, overview: { ...change.overview, current: null } };

    await deleteShareGroup(TOKEN, 'alpha');
    expect(fetchMock()).toHaveBeenCalledExactlyOnceWith('/v1/share-groups/alpha', {
      method: 'DELETE',
      headers: WRITE_HEADERS,
    });
  });

  it('应答什么都没有时也给齐四个字段，归档写成空串', async () => {
    body = {};

    await expect(createShareGroup(TOKEN, 'alpha')).resolves.toEqual({
      group: '', archived: '', copied_env: [], overview: EMPTY,
    });
  });

  it('归档路径不是串、抄过去的密钥不是数组，都按缺省算', async () => {
    body = { group: 'alpha', archived: null, copied_env: 'OPENAI_API_KEY', overview: { available: true } };

    await expect(joinShareGroup(TOKEN, 'alpha')).resolves.toEqual({
      group: 'alpha',
      archived: '',
      copied_env: [],
      overview: { ...EMPTY, available: true },
    });
  });

  it('改动结果里的全貌同样走归一，页面拿到就能直接渲染', async () => {
    body = {
      group: 'alpha',
      overview: { available: true, current: 'alpha', groups: [{ name: 'alpha', members: [{ id: 'main', current: true }] }] },
    };

    await expect(joinShareGroup(TOKEN, 'alpha')).resolves.toEqual({
      group: 'alpha',
      archived: '',
      copied_env: [],
      overview: {
        available: true,
        reason: '',
        current: 'alpha',
        groups: [{
          name: 'alpha',
          members: [{ id: 'main', label: 'main', current: true }],
          created_at: '',
          created_by: '',
        }],
        archives: [],
      },
    });
  });

  it('后端拒绝建组时把原因抛出来', async () => {
    status = 409;
    body = { detail: '已经有叫 alpha 的组' };

    await expect(createShareGroup(TOKEN, 'alpha')).rejects.toThrow('409 已经有叫 alpha 的组');
  });
});
