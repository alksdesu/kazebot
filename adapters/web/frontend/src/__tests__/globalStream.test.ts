// 全局事件流的连接管理：首帧带令牌认证，认过才算连上；断开后按指数退避重连，后端要求等多久就等多久，
// 令牌被拒不再重连；连上过之后再连上要告诉调用方去补拉，彻底断开就不再重连。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { connectGlobalWS, disconnectGlobalWS } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import type { SupervisorEvent } from '../types/chat';

class FakeSocket {
  static instances: FakeSocket[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 0;
  sent: string[] = [];

  constructor(public readonly url: string) {
    FakeSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close(code = 1006, reason = '') {
    this.readyState = 3;
    this.onclose?.({ code, reason } as CloseEvent);
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  /** 连上并通过认证：后端认过首帧里的令牌后先发 stream_ready。 */
  accept() {
    this.open();
    this.receive(JSON.stringify({ type: 'stream_ready' }));
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
  useSettingsStore.setState({ adminToken: 'stream-token' });
});

afterEach(() => {
  disconnectGlobalWS();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  useSettingsStore.setState({ adminToken: null });
});

describe('全局事件流重连', () => {
  it('每次失败等的时间翻倍，连上过一次就从头算', () => {
    const onOpen = vi.fn();
    const onClose = vi.fn();
    connectGlobalWS({ onEvent: () => {}, onOpen, onClose });
    latest().accept();
    expect(onOpen).toHaveBeenLastCalledWith({ reconnected: false });
    expect(latest().sent).toEqual([JSON.stringify({ last_seq: 0, token: 'stream-token' })]);

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

    latest().accept();
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
    latest().accept();

    connectGlobalWS({ onEvent: second });
    latest().receive(JSON.stringify(EVENT));

    expect(FakeSocket.instances).toHaveLength(1);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith(EVENT);
  });

  it('彻底断开之后不再重连，下次连上算第一次', () => {
    const onOpen = vi.fn();
    connectGlobalWS({ onEvent: () => {}, onOpen });
    latest().accept();

    disconnectGlobalWS();
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);

    connectGlobalWS({ onEvent: () => {}, onOpen });
    latest().accept();
    expect(onOpen).toHaveBeenLastCalledWith({ reconnected: false });
  });

  it('心跳、认证通过的信号和认不出的帧不交给调用方', () => {
    const onEvent = vi.fn();
    connectGlobalWS({ onEvent });
    latest().accept();

    latest().receive('not json');
    latest().receive(JSON.stringify([EVENT]));
    latest().receive(JSON.stringify({ ...EVENT, type: 42 }));
    latest().receive(JSON.stringify(null));
    latest().receive(JSON.stringify({ type: 'ping' }));
    latest().receive(JSON.stringify({ type: 'stream_ready' }));
    latest().receive(JSON.stringify(EVENT));

    expect(onEvent.mock.calls).toEqual([[EVENT]]);
  });
});

describe('全局事件流认证', () => {
  it('首帧带上当前登录的令牌', () => {
    connectGlobalWS({ onEvent: () => {} });
    latest().open();

    expect(latest().sent).toEqual([JSON.stringify({ last_seq: 0, token: 'stream-token' })]);
  });

  // 被拒的连接也是先 open 再关：open 一触发就清零的话，令牌不对时会每秒重连一次。
  it('后端认过令牌之前不算连上，退避也不清零', () => {
    const onOpen = vi.fn();
    const onClose = vi.fn();
    connectGlobalWS({ onEvent: () => {}, onOpen, onClose });
    for (let attempt = 0; attempt < 3; attempt += 1) {
      latest().open();
      latest().close();
      vi.advanceTimersByTime(30_000);
    }

    expect(onOpen).not.toHaveBeenCalled();
    expect(onClose.mock.calls.map(([info]) => info.retryInMs)).toEqual([1000, 2000, 4000]);
  });

  it('先到的心跳或事件也说明认证过了', () => {
    const onOpen = vi.fn();
    const onEvent = vi.fn();
    connectGlobalWS({ onEvent, onOpen });
    latest().open();
    expect(onOpen).not.toHaveBeenCalled();

    latest().receive(JSON.stringify(EVENT));
    expect(onOpen).toHaveBeenCalledExactlyOnceWith({ reconnected: false });
    expect(onEvent).toHaveBeenCalledWith(EVENT);
    latest().receive(JSON.stringify({ type: 'stream_ready' }));
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it('令牌被拒（4401）就不再重连，交给调用方去让人重新登录', () => {
    const onClose = vi.fn();
    const onUnauthorized = vi.fn();
    connectGlobalWS({ onEvent: () => {}, onClose, onUnauthorized });
    latest().open();
    latest().close(4401, 'unauthorized');

    expect(onUnauthorized).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
    vi.advanceTimersByTime(120_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it('后端正在退避（4429）就按 reason 里的秒数再连', () => {
    const onClose = vi.fn();
    connectGlobalWS({ onEvent: () => {}, onClose });
    latest().open();
    latest().close(4429, 'retry after 12 seconds');

    expect(onClose).toHaveBeenLastCalledWith({ retryInMs: 12_000 });
    vi.advanceTimersByTime(11_999);
    expect(FakeSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it('4429 的 reason 里读不出秒数，照平常的退避算', () => {
    const onClose = vi.fn();
    connectGlobalWS({ onEvent: () => {}, onClose });
    latest().open();
    latest().close(4429, 'too many failures');

    expect(onClose).toHaveBeenLastCalledWith({ retryInMs: 1000 });
  });
});
