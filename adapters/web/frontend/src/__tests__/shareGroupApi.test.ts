// 共享配置组的客户端：打错一条 URL 就会把别的组的配置换给本实例，所以每条路径、
// 方法和请求体都钉住；应答对不上形状就报格式不对、说出位置，补成空的会把「没读懂」说成「没有组」。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  createShareGroup,
  deleteShareGroup,
  getShareGroups,
  isValidShareGroupName,
  joinShareGroup,
  leaveShareGroup,
  ReceiptUnreadable,
  type ShareGroupOverview,
} from '../api/supervisorClient';

const TOKEN = 'admin-token';
const AUTH = { Authorization: `Bearer ${TOKEN}` };
const WRITE_HEADERS = { 'Content-Type': 'application/json', ...AUTH };

const OVERVIEW: ShareGroupOverview = {
  available: true,
  reason: '',
  current: 'alpha',
  groups: [{
    name: 'alpha',
    members: [{ id: 'main', label: '主实例', current: true }, { id: 'xiaoyu', label: '第二实例', current: false }],
    created_at: '2026-01-02T03:04:05Z',
    created_by: '主实例',
  }],
  archives: ['data/config.archived-1'],
};

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

  it('组、成员、建组信息和归档照原样读出来，多出来的字段不带进全貌', async () => {
    body = {
      ...OVERVIEW,
      groups: OVERVIEW.groups.map(group => ({
        ...group,
        members: group.members.map(member => ({ ...member, path: '/i/x' })),
        meta: {},
      })),
    };

    await expect(getShareGroups(TOKEN)).resolves.toEqual(OVERVIEW);
  });

  // 成员只能从实例清单认：清单读不懂时各组 members 是空的，得带着原因，页面才不会说成没人在用。
  it('实例清单读不懂时带上查不了成员的原因', async () => {
    const reason = '实例清单 /srv/kazebot-data/instances.yaml 读不懂：第 3 行第 31 列：mapping values are not allowed here';
    body = { ...OVERVIEW, groups: [{ ...OVERVIEW.groups[0], members: [] }], members_problem: reason };

    const overview = await getShareGroups(TOKEN);

    expect(overview.members_problem).toBe(reason);
    expect(overview.groups[0].members).toEqual([]);
  });

  it('老后端不给查成员的原因时按查得了算', async () => {
    body = OVERVIEW;

    expect((await getShareGroups(TOKEN)).members_problem).toBeUndefined();
  });

  it('空的当前组算没进组', async () => {
    body = { ...OVERVIEW, current: '' };

    expect((await getShareGroups(TOKEN)).current).toBeNull();
  });

  // 补成一份空全貌的话，面板会说「还没有组」「不能共享」，其实是没读懂。
  it.each([
    [{}, '缺 available'],
    [[], '返回的不是 JSON 对象'],
    [{ ...OVERVIEW, available: 1 }, 'available 不是 true 或 false'],
    [{ ...OVERVIEW, reason: 42 }, 'reason 不是字符串'],
    [{ ...OVERVIEW, current: 7 }, 'current 不是字符串，也不是 null'],
    [{ ...OVERVIEW, members_problem: 7 }, 'members_problem 不是字符串'],
    [{ ...OVERVIEW, groups: [...OVERVIEW.groups, null] }, 'groups[1] 不是 JSON 对象'],
    [{ ...OVERVIEW, groups: [{ ...OVERVIEW.groups[0], name: '' }] }, 'groups[0].name 不是非空字符串'],
    [{ ...OVERVIEW, groups: [{ ...OVERVIEW.groups[0], members: 'nope' }] }, 'groups[0].members 不是数组'],
    [{ ...OVERVIEW, groups: [{ ...OVERVIEW.groups[0], members: [{ id: 'main', current: true }] }] }, '缺 groups[0].members[0].label'],
    [{ ...OVERVIEW, groups: [{ ...OVERVIEW.groups[0], created_at: 7 }] }, 'groups[0].created_at 不是字符串'],
    [{ ...OVERVIEW, archives: ['data/config.archived-1', 7] }, 'archives 不是字符串数组'],
  ])('应答对不上形状时报格式不对，说出是哪一项（%j）', async (received, problem) => {
    body = received;

    await expect(getShareGroups(TOKEN)).rejects.toThrow(`共享配置组格式不对：${problem}`);
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
      headers: AUTH,
    });
  });

  it('退出的回执里没有归档和抄过去的密钥，按没有算', async () => {
    body = { group: 'alpha', overview: { ...OVERVIEW, current: null } };

    await expect(leaveShareGroup(TOKEN)).resolves.toEqual({
      group: 'alpha', archived: '', copied_env: [], overview: { ...OVERVIEW, current: null },
    });
  });

  it('回执里的全貌和单独读的一样逐项核对、只带认得的字段', async () => {
    body = { ...change, overview: { ...OVERVIEW, extra: true } };

    await expect(joinShareGroup(TOKEN, 'alpha')).resolves.toEqual({ ...change, overview: OVERVIEW });
  });

  // 回执是改完才发的：读不懂也得先交代已经做完，免得人当成失败再点一次。
  it.each([
    [{}, '缺 group'],
    [{ ...change, group: '' }, 'group 不是非空字符串'],
    [{ ...change, archived: null }, 'archived 不是字符串'],
    [{ ...change, copied_env: 'OPENAI_API_KEY' }, 'copied_env 不是字符串数组'],
    [{ group: 'alpha' }, '缺 overview'],
    [{ ...change, overview: { ...OVERVIEW, reason: undefined } }, '缺 overview.reason'],
    [
      { ...change, overview: { ...OVERVIEW, groups: [{ ...OVERVIEW.groups[0], members: [{ id: 'main', current: 'yes' }] }] } },
      '缺 overview.groups[0].members[0].label',
    ],
  ])('回执对不上形状时说操作已经做完、回执哪一项不对（%j）', async (received, problem) => {
    body = received;

    await expect(createShareGroup(TOKEN, 'alpha')).rejects.toThrow(`共享配置组的操作已经做完，可回执格式不对：${problem}`);
  });

  it('回执读不懂抛的是 ReceiptUnreadable：页面凭它认出操作已经做完，照常收尾', async () => {
    body = {};

    await expect(createShareGroup(TOKEN, 'alpha')).rejects.toBeInstanceOf(ReceiptUnreadable);
  });

  it('回执不是 JSON 也是操作做完了、回执读不懂', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>ok</html>', {
      status: 200, headers: { 'Content-Type': 'text/html' },
    })));

    const failure = await joinShareGroup(TOKEN, 'alpha').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ReceiptUnreadable);
    expect((failure as Error).message).toBe('共享配置组的操作已经做完，可回执不是 JSON');
  });

  it('后端拒绝建组时把原因抛出来', async () => {
    status = 409;
    body = { detail: '已经有叫 alpha 的组' };

    await expect(createShareGroup(TOKEN, 'alpha')).rejects.toThrow('409 已经有叫 alpha 的组');
  });

  it('后端拒绝是请求失败，不能当成回执读不懂去重启 engine', async () => {
    status = 409;
    body = { detail: '已经有叫 alpha 的组' };

    const failure = await createShareGroup(TOKEN, 'alpha').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).not.toBeInstanceOf(ReceiptUnreadable);
  });
});
