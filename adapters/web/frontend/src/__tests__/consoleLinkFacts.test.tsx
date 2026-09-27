// 链路实况读 live 状态约定里的顶层几项，不读平台 runtime 里自己的同名字段。
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { ReactNode } from 'react';

import type { ConfigLiveState } from '../api/supervisorClient';
import { ConnectionCard } from '../console/ConnectionCard';
import { platformConsoleStore, readLinkFacts, type ConsoleState } from '../console/consoleStore';
import { nameBetween, nameWith } from '../console/format';
import { QueueFacts } from '../console/LinkFacts';
import { PlatformProvider } from '../console/platformContext';
import { AccountSummary } from '../console/sectionSummaries';
import { QQ_NAPCAT, type PlatformDescriptor } from '../platforms/registry';

// 后端新出的平台：前端没登记过，连接卡上的两行名字全从描述里来。
const OTHERCHAT: PlatformDescriptor = {
  id: 'otherchat',
  displayName: 'OtherChat',
  shortName: 'Other',
  channels: [{ name: 'other_group', kind: 'group', hashed: true }],
  hasConfig: true,
  entrypoints: [],
  connectionPeer: 'OtherChat 网关',
  connectionHint: '检查网关地址与接入密钥',
};

// 简称和对端都是中文的平台：接中文时不该多一个空格。
const WECHAT: PlatformDescriptor = {
  id: 'wechat',
  displayName: '微信',
  shortName: '微信',
  channels: [{ name: 'wechat_group', kind: 'group', hashed: true }],
  hasConfig: true,
  entrypoints: [],
  connectionPeer: '微信网关',
  connectionHint: '检查扫码状态',
};

const live = (state: NonNullable<ConfigLiveState['state']>, stale = false): ConfigLiveState => ({
  published: true,
  stale,
  applied: true,
  file: { exists: true, mtime_ns: 1, size: 1 },
  state,
});

const inQq = (child: ReactNode) => render(<PlatformProvider platform={QQ_NAPCAT}>{child}</PlatformProvider>);

const inPlatform = (platform: PlatformDescriptor, child: ReactNode) =>
  render(<PlatformProvider platform={platform}>{child}</PlatformProvider>);

const seed = (value: ConfigLiveState | null) => platformConsoleStore('qq').setState({ live: value });

const seedFor = (platformId: string, value: ConfigLiveState | null) =>
  platformConsoleStore(platformId).setState({ live: value });

beforeEach(() => {
  seed(null);
  seedFor('otherchat', null);
  seedFor('wechat', null);
});
afterEach(cleanup);

// 「NapCat 已连接」要空格，「平台已连接」不要 —— 中英之间空一格，中文之间空就是错字。
describe('名字后面接一句中文', () => {
  it('拉丁字母或数字结尾时隔一个空格', () => {
    expect(nameWith('NapCat', '已连接')).toBe('NapCat 已连接');
    expect(nameWith('QQ', '适配进程')).toBe('QQ 适配进程');
    expect(nameWith('OneBot11', '已连接')).toBe('OneBot11 已连接');
  });

  it('汉字结尾时直接连上', () => {
    expect(nameWith('平台', '已连接')).toBe('平台已连接');
    expect(nameWith('微信', '适配进程')).toBe('微信适配进程');
    expect(nameWith('OtherChat 网关', '未连接')).toBe('OtherChat 网关未连接');
  });

  it('全角标点结尾也算中文那一侧', () => {
    expect(nameWith('主号（备）', '已连接')).toBe('主号（备）已连接');
    expect(nameWith('网关！', '未连接')).toBe('网关！未连接');
    expect(nameWith('网关。', '已连接')).toBe('网关。已连接');
    expect(nameWith('「主号」', '已连接')).toBe('「主号」已连接');
  });

  it('名字是空的就只剩那句话，不留一个先导空格', () => {
    expect(nameWith('', '已连接')).toBe('已连接');
  });

  it('那句话是空的就只剩名字，不留一个尾随空格', () => {
    expect(nameWith('NapCat', '')).toBe('NapCat');
    expect(nameWith('微信', '')).toBe('微信');
    expect(nameWith('', '')).toBe('');
  });

  it('扩展区的汉字占两个 UTF-16 单元，按码点判才认得出来', () => {
    expect(nameWith('\u{20BB7}号', '已连接')).toBe('\u{20BB7}号已连接');
    expect(nameWith('网关\u{20BB7}', '已连接')).toBe('网关\u{20BB7}已连接');
    expect(nameWith('\u{20000}', '未连接')).toBe('\u{20000}未连接');
    expect(nameWith('\u{30EDE}', '已连接')).toBe('\u{30EDE}已连接');
  });

  it('整个名字只有一个代理对字符时，两边都按码点判', () => {
    expect(nameWith('\u{20BB7}', '已连接')).toBe('\u{20BB7}已连接');
    expect(nameWith('\u{1F680}', '已连接')).toBe('\u{1F680} 已连接');
  });

  it('emoji 和别的非中文符号按拉丁那边处理', () => {
    expect(nameWith('网关🚀', '已连接')).toBe('网关🚀 已连接');
    expect(nameWith('gateway-1', '已连接')).toBe('gateway-1 已连接');
    expect(nameWith('Кот', '已连接')).toBe('Кот 已连接');
    // 半角间隔号是拉丁扩展里的，和全角标点不是一回事。
    expect(nameWith('QQ·NapCat·', '已连接')).toBe('QQ·NapCat· 已连接');
  });
});

// 「这个 QQ 号」要空格，「这个微信号」不要：名字夹在两段中文中间，两边各按它挨着的那一头判。
describe('名字夹在两段中文之间', () => {
  it('拉丁名两边各空一个', () => {
    expect(nameBetween('这个', 'QQ', '号')).toBe('这个 QQ 号');
    expect(nameBetween('启动', 'OtherChat', '的适配器进程')).toBe('启动 OtherChat 的适配器进程');
  });

  it('汉字名两边都不空', () => {
    expect(nameBetween('这个', '微信', '号')).toBe('这个微信号');
    expect(nameBetween('启动', '保险库', '的适配器进程')).toBe('启动保险库的适配器进程');
  });

  it('首尾各判各的', () => {
    expect(nameBetween('这个', 'QQ群', '里')).toBe('这个 QQ群里');
    expect(nameBetween('这个', '微信 Work', '号')).toBe('这个微信 Work 号');
  });

  it('前半段是空的就不留先导空格', () => {
    expect(nameBetween('', 'QQ', '号')).toBe('QQ 号');
    expect(nameBetween('', '微信', '号')).toBe('微信号');
  });

  it('后半段是空的就到名字为止', () => {
    expect(nameBetween('这个', 'QQ', '')).toBe('这个 QQ');
    expect(nameBetween('这个', '微信', '')).toBe('这个微信');
  });

  it('名字是空的两段直接接上', () => {
    expect(nameBetween('这个', '', '号')).toBe('这个号');
  });

  it('扩展区的汉字开头也按码点判', () => {
    expect(nameBetween('这个', '\u{20BB7}号', '群')).toBe('这个\u{20BB7}号群');
    expect(nameBetween('这个', '\u{1F680}Chat', '号')).toBe('这个 \u{1F680}Chat 号');
  });
});

describe('连接卡', () => {
  it('按约定里的 connected 报平台那一段', () => {
    seed(live({ connected: true, runtime: { pid: 42 } }));
    inQq(<ConnectionCard />);

    expect(screen.getByText('已连接')).toBeTruthy();
    expect(screen.getByText('pid 42')).toBeTruthy();
  });

  it('没报 connected 就是没连上', () => {
    seed(live({ runtime: { pid: 42 } }));
    inQq(<ConnectionCard />);

    expect(screen.getByText('未连接')).toBeTruthy();
  });

  it('心跳断了平台那一段无从判断', () => {
    seed(live({ connected: true }, true));
    inQq(<ConnectionCard />);

    expect(screen.getByText('心跳中断')).toBeTruthy();
    expect(screen.getByText('未知')).toBeTruthy();
  });

  it('两行名字都从平台描述里来，QQ 的还是原来那两个', () => {
    seed(live({ connected: true }));
    inQq(<ConnectionCard />);

    expect(screen.getByText('QQ 适配进程')).toBeTruthy();
    expect(screen.getByText('NapCat')).toBeTruthy();
  });

  it('换个平台就换个叫法，不再写死 QQ 和 NapCat', () => {
    seedFor('otherchat', live({ connected: true }));
    inPlatform(OTHERCHAT, <ConnectionCard />);

    expect(screen.getByText('Other 适配进程')).toBeTruthy();
    expect(screen.getByText('OtherChat 网关')).toBeTruthy();
  });

  it('简称是中文时「适配进程」直接连上，不夹一个空格', () => {
    seedFor('wechat', live({ connected: true }));
    inPlatform(WECHAT, <ConnectionCard />);

    expect(screen.getByText('微信适配进程')).toBeTruthy();
    expect(screen.queryByText('微信 适配进程')).toBeNull();
  });

  it('平台没说连的是谁，那一行就叫「平台」', () => {
    seedFor('otherchat', live({ connected: true }));
    inPlatform({ ...OTHERCHAT, connectionPeer: '' }, <ConnectionCard />);

    expect(screen.getByText('平台')).toBeTruthy();
  });

  it('进程在跑但没连上时，把这个平台该先查什么摆出来', () => {
    seedFor('otherchat', live({ connected: false }));
    inPlatform(OTHERCHAT, <ConnectionCard />);

    expect(screen.getByText('未连接')).toBeTruthy();
    expect(screen.getByText('检查网关地址与接入密钥')).toBeTruthy();
  });

  it('连上了就不再提示查什么，进程没在跑时这一项无从判断', () => {
    seedFor('otherchat', live({ connected: true }));
    const view = inPlatform(OTHERCHAT, <ConnectionCard />);
    expect(screen.queryByText('检查网关地址与接入密钥')).toBeNull();

    view.unmount();
    seedFor('otherchat', null);
    inPlatform(OTHERCHAT, <ConnectionCard />);

    expect(screen.getByText('适配进程没在跑，这一项无从判断')).toBeTruthy();
    expect(screen.queryByText('检查网关地址与接入密钥')).toBeNull();
  });
});

describe('账号分区的摘要', () => {
  it('在线时说 NapCat 连没连上', () => {
    seed(live({ connected: true }));
    inQq(<AccountSummary />);

    expect(screen.getByText('适配进程在线 · NapCat 已连接')).toBeTruthy();
  });

  it('没连上照实说', () => {
    seed(live({ connected: false }));
    inQq(<AccountSummary />);

    expect(screen.getByText('适配进程在线 · NapCat 未连接')).toBeTruthy();
  });

  it('别的平台按它自己的对端说，对端以汉字收尾就不留空格', () => {
    seedFor('otherchat', live({ connected: true }));
    inPlatform(OTHERCHAT, <AccountSummary />);

    expect(screen.getByText('适配进程在线 · OtherChat 网关已连接')).toBeTruthy();
  });

  it('平台没说连的是谁，摘要里也叫「平台」', () => {
    seedFor('otherchat', live({ connected: false }));
    inPlatform({ ...OTHERCHAT, connectionPeer: '' }, <AccountSummary />);

    expect(screen.getByText('适配进程在线 · 平台未连接')).toBeTruthy();
  });
});

describe('队列实况', () => {
  it('积压条数取约定里的 backlog', () => {
    seed(live({
      backlog: 3,
      values: { enable_queue: true, queue_workers: 2 },
      runtime: { queue_workers_running: 2 },
    }));
    inQq(<QueueFacts />);

    expect(screen.getByText(/队列里积压 3 条/)).toBeTruthy();
  });
});

describe('readLinkFacts', () => {
  const read = (state: NonNullable<ConfigLiveState['state']>) =>
    readLinkFacts({ live: live(state) } as unknown as ConsoleState);

  it('读出三项', () => {
    expect(read({ connected: true, account: '10001', backlog: 4 })).toEqual({ connected: true, account: '10001', backlog: 4 });
  });

  it('形状不对的值当没有', () => {
    const odd = { connected: 'yes', account: 10001, backlog: -2 } as unknown as NonNullable<ConfigLiveState['state']>;
    expect(read(odd)).toEqual({ connected: false, account: '', backlog: 0 });
    const junk = { backlog: 'many' } as unknown as NonNullable<ConfigLiveState['state']>;
    expect(read(junk).backlog).toBe(0);
  });

  it('bot 没公布时全是空的', () => {
    expect(readLinkFacts({ live: null } as unknown as ConsoleState)).toEqual({ connected: false, account: '', backlog: 0 });
  });
});
