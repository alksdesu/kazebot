// 全局事件流的连接管理：断开后按指数退避重连，连上过之后再连上要告诉调用方去补拉，彻底断开就不再重连。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { connectGlobalWS, disconnectGlobalWS } from '../api/supervisorClient';
import type { SupervisorEvent } from '../types/chat';

class FakeSocket {
  static instances: FakeSocket[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 0;
  sent: string[] = [];

  constructor(public readonly url: string) {
    FakeSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = 3;
    this.onclose?.();
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  receive(data: string) {
    this.onmessage?.({ data } as MessageEvent<string>);
  }
}

const latest = (): FakeSocket => FakeSocket.instances[FakeSocket.instances.length - 1];

const EVENT: SupervisorEvent = {
  seq: 1,
  event_id: 'ev-1',
  ts: '2026-05-31T03:10:00.000Z',
  session_id: 'sess-a',
  type: 'stream_delta',
  payload: { type: 'text', content: 'hi' },
};

beforeEach(() => {
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket);
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
});

afterEach(() => {
  disconnectGlobalWS();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('全局事件流重连', () => {
  it('每次失败等的时间翻倍，连上过一次就从头算', () => {
    const onOpen = vi.fn();
    const onClose = vi.fn();
    connectGlobalWS({ onEvent: () => {}, onOpen, onClose });
    latest().open();
    expect(onOpen).toHaveBeenLastCalledWith({ reconnected: false });
    expect(latest().sent).toEqual([JSON.stringify({ last_seq: 0 })]);

    latest().close();
    expect(onClose).toHaveBeenLastCalledWith({ retryInMs: 1000 });
    vi.advanceTimersByTime(999);
    expect(FakeSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(2);

    latest().close();
    expect(onClose).toHaveBeenLastCalledWith({ retryInMs: 2000 });
    vi.advanceTimersByTime(2000);
    latest().close();
    expect(onClose).toHaveBeenLastCalledWith({ retryInMs: 4000 });
    vi.advanceTimersByTime(4000);

    latest().open();
    expect(onOpen).toHaveBeenLastCalledWith({ reconnected: true });
    latest().close();
    expect(onClose).toHaveBeenLastCalledWith({ retryInMs: 1000 });
  });

  it('等待最长三十秒，不再往上翻', () => {
    const onClose = vi.fn();
    connectGlobalWS({ onEvent: () => {}, onClose });
    for (let attempt = 0; attempt < 8; attempt += 1) {
      latest().close();
      vi.advanceTimersByTime(30_000);
    }

    expect(onClose.mock.calls.map(([info]) => info.retryInMs)).toEqual([1000, 2000, 4000, 8000, 16_000, 30_000, 30_000, 30_000]);
  });

  it('等重连的时候再要连接就立刻重试，不会多出一条', () => {
    connectGlobalWS({ onEvent: () => {} });
    latest().close();

    connectGlobalWS({ onEvent: () => {} });
    expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it('已经连着时再要连接只换回调', () => {
    const first = vi.fn();
    const second = vi.fn();
    connectGlobalWS({ onEvent: first });
    latest().open();

    connectGlobalWS({ onEvent: second });
    latest().receive(JSON.stringify(EVENT));

    expect(FakeSocket.instances).toHaveLength(1);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith(EVENT);
  });

  it('彻底断开之后不再重连，下次连上算第一次', () => {
    const onOpen = vi.fn();
    connectGlobalWS({ onEvent: () => {}, onOpen });
    latest().open();

    disconnectGlobalWS();
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);

    connectGlobalWS({ onEvent: () => {}, onOpen });
    latest().open();
    expect(onOpen).toHaveBeenLastCalledWith({ reconnected: false });
  });

  it('心跳和坏帧不交给调用方', () => {
    const onEvent = vi.fn();
    connectGlobalWS({ onEvent });
    latest().open();

    latest().receive('not json');
    latest().receive(JSON.stringify({ type: 'ping' }));
    latest().receive(JSON.stringify(EVENT));

    expect(onEvent.mock.calls).toEqual([[EVENT]]);
  });
});
