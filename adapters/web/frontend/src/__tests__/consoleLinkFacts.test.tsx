// 链路实况读 live 状态约定里的顶层几项，不读平台 runtime 里自己的同名字段。
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { ReactNode } from 'react';

import type { ConfigLiveState } from '../api/supervisorClient';
import { ConnectionCard } from '../console/ConnectionCard';
import { platformConsoleStore, readLinkFacts, type ConsoleState } from '../console/consoleStore';
import { QueueFacts } from '../console/LinkFacts';
import { PlatformProvider } from '../console/platformContext';
import { AccountSummary } from '../console/sectionSummaries';
import { QQ_NAPCAT } from '../platforms/registry';

const live = (state: NonNullable<ConfigLiveState['state']>, stale = false): ConfigLiveState => ({
  published: true,
  stale,
  applied: true,
  file: { exists: true, mtime_ns: 1, size: 1 },
  state,
});

const inQq = (child: ReactNode) => render(<PlatformProvider platform={QQ_NAPCAT}>{child}</PlatformProvider>);

const seed = (value: ConfigLiveState | null) => platformConsoleStore('qq').setState({ live: value });

beforeEach(() => seed(null));
afterEach(cleanup);

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
