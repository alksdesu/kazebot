// 试听的正确性只有一个判据：喂给后端的必须是「编辑中的配置」，判定必须挂回对的那条消息。
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { QqDryRunVerdict } from '../api/supervisorClient';
import {
  alignVerdicts,
  AUDITION_GROUP,
  MAX_MESSAGES,
  newMessage,
  SENDERS,
  toRequest,
  useAuditionStore,
} from '../console/auditionStore';
import { useConsoleStore } from '../console/consoleStore';

const PATHS: Record<string, string> = {
  signal_at: 'trigger.signals.at',
  signal_name: 'trigger.signals.name',
  name_words: 'trigger.name.words',
};

const seedLive = (values: Record<string, unknown>) => {
  useConsoleStore.setState({
    live: {
      published: true,
      applied: true,
      file: { exists: true, mtime_ns: 1, size: 1 },
      state: { values, paths: PATHS },
    },
    draft: {},
  });
};

const verdict = (index: number, extra: Partial<QqDryRunVerdict> = {}): QqDryRunVerdict => ({
  index,
  text: '',
  triggered: false,
  signal: '',
  blocked_by: '',
  reason: '',
  undetermined: false,
  cooldown_remaining: 0,
  ...extra,
});

describe('toRequest', () => {
  it('spaces the messages out so a cooldown window can be seen', () => {
    const out = toRequest([newMessage({ text: 'a' }), newMessage({ text: 'b' })], 5);

    expect(out.map((m) => m.at_sec)).toEqual([0, 5]);
  });

  it('collapses to a single instant when the interval is zero', () => {
    // 间隔 0 = 「同一瞬间涌进来」，是压冷却的场景，不该被当成非法输入。
    const out = toRequest([newMessage(), newMessage()], 0);

    expect(out.map((m) => m.at_sec)).toEqual([0, 0]);
  });

  it('treats a nonsense interval as zero rather than emitting NaN', () => {
    const out = toRequest([newMessage(), newMessage()], Number.NaN);

    expect(out.every((m) => Number.isFinite(m.at_sec))).toBe(true);
  });

  it('gives the two senders different user ids', () => {
    // 「每人冷却」按 (群, 人) 分组，两个发言人共用一个 id 就演示不出来。
    const out = toRequest([newMessage({ sender: 'a' }), newMessage({ sender: 'b' })], 1);

    expect(out[0].user_id).not.toBe(out[1].user_id);
    expect(out[0].group_id).toBe(out[1].group_id);
    expect(out[0].user_id).toBe(SENDERS.a.userId);
  });

  it('passes the at / reply facts through untouched', () => {
    const out = toRequest([newMessage({ atMe: true, replyToBot: true })], 1);

    expect(out[0]).toMatchObject({ at_me: true, reply_to_bot: true });
  });
});

describe('alignVerdicts', () => {
  it('matches by the id the question was asked about', () => {
    const map = alignVerdicts(['m1', 'm2'], [verdict(0, { triggered: true }), verdict(1)]);

    expect(map.get('m1')?.triggered).toBe(true);
    expect(map.get('m2')?.triggered).toBe(false);
  });

  it('drops a verdict whose message is gone', () => {
    // 删掉一条之后 index 会整体错位，按 index 渲染就会把判定挂到别人身上。
    const map = alignVerdicts(['m1'], [verdict(0), verdict(1), verdict(2)]);

    expect(map.size).toBe(1);
    expect(map.has('m1')).toBe(true);
  });

  it('survives a missing result payload', () => {
    expect(alignVerdicts(['m1'], undefined).size).toBe(0);
  });
});

describe('the message list', () => {
  beforeEach(() => {
    useAuditionStore.setState({ messages: [newMessage({ text: 'seed' })], result: null, resultIds: [] });
  });

  it('explodes a pasted block into one message per line', () => {
    const id = useAuditionStore.getState().messages[0].id;

    useAuditionStore.getState().explode(id, ['一句', '两句', '三句']);

    expect(useAuditionStore.getState().messages.map((m) => m.text)).toEqual(['一句', '两句', '三句']);
  });

  it('keeps the sender when exploding', () => {
    useAuditionStore.setState({ messages: [newMessage({ sender: 'b' })] });
    const id = useAuditionStore.getState().messages[0].id;

    useAuditionStore.getState().explode(id, ['x', 'y']);

    expect(useAuditionStore.getState().messages.every((m) => m.sender === 'b')).toBe(true);
  });

  it('honours the ceiling when a huge block is pasted', () => {
    const id = useAuditionStore.getState().messages[0].id;

    useAuditionStore.getState().explode(id, Array.from({ length: 400 }, (_, i) => `line ${i}`));

    expect(useAuditionStore.getState().messages.length).toBe(MAX_MESSAGES);
  });

  it('refuses to grow past the ceiling one at a time', () => {
    useAuditionStore.setState({
      messages: Array.from({ length: MAX_MESSAGES }, () => newMessage()),
    });

    useAuditionStore.getState().add();

    expect(useAuditionStore.getState().messages.length).toBe(MAX_MESSAGES);
  });

  it('ignores an explode aimed at a message that no longer exists', () => {
    useAuditionStore.getState().explode('nope', ['a', 'b']);

    expect(useAuditionStore.getState().messages.map((m) => m.text)).toEqual(['seed']);
  });
});

describe('run', () => {
  beforeEach(() => {
    useConsoleStore.setState({ draft: {}, live: null });
    useAuditionStore.setState({
      messages: [newMessage({ text: '咪啪在吗' })],
      result: null,
      resultIds: [],
      error: '',
      cooldown: true,
      intervalSec: 1,
    });
  });

  const stubFetch = (capture: { body?: any }) => {
    const original = globalThis.fetch;
    globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      capture.body = JSON.parse(String(init?.body));
      return new Response(
        JSON.stringify({ enabled_signals: ['at'], cooldown_applied: true, results: [verdict(0)] }),
        { headers: { 'Content-Type': 'application/json' } },
      );
    }) as typeof fetch;
    return () => { globalThis.fetch = original; };
  };

  it('sends the draft on top of the published values', async () => {
    // 只发 live 的值就成了「试听旧配置」—— 改开关看不到判定翻转，试听也就没用了。
    seedLive({ signal_at: true, signal_name: false });
    useConsoleStore.getState().setDraft('signal_name', true);
    const capture: { body?: any } = {};
    const restore = stubFetch(capture);

    try {
      await useAuditionStore.getState().run('token');
    } finally {
      restore();
    }

    expect(capture.body.config).toMatchObject({ signal_at: true, signal_name: true });
  });

  it('keeps a tri-state null intact all the way to the request', async () => {
    // null 是「这一项没配，跟随旧的 group_mode」。被压成 false 就等于悄悄接管。
    seedLive({ signal_at: null });
    const capture: { body?: any } = {};
    const restore = stubFetch(capture);

    try {
      await useAuditionStore.getState().run('token');
    } finally {
      restore();
    }

    expect(capture.body.config).toHaveProperty('signal_at', null);
  });

  it('records which messages the answer belongs to', async () => {
    seedLive({ signal_at: true });
    const askedId = useAuditionStore.getState().messages[0].id;
    const restore = stubFetch({});

    try {
      await useAuditionStore.getState().run('token');
    } finally {
      restore();
    }

    expect(useAuditionStore.getState().resultIds).toEqual([askedId]);
  });

  it('discards a stale response that lands after a newer one', async () => {
    seedLive({ signal_at: true });
    const original = globalThis.fetch;
    let call = 0;
    globalThis.fetch = (async () => {
      call += 1;
      const mine = call;
      // 第一发慢、第二发快 —— 不做守卫的话界面最后显示的是上一份配置的判定。
      await new Promise((resolve) => setTimeout(resolve, mine === 1 ? 30 : 0));
      return new Response(
        JSON.stringify({
          enabled_signals: [mine === 1 ? 'stale' : 'fresh'],
          cooldown_applied: true,
          results: [verdict(0)],
        }),
        { headers: { 'Content-Type': 'application/json' } },
      );
    }) as typeof fetch;

    try {
      const slow = useAuditionStore.getState().run('token');
      const fast = useAuditionStore.getState().run('token');
      await Promise.all([slow, fast]);
    } finally {
      globalThis.fetch = original;
    }

    expect(useAuditionStore.getState().result?.enabled_signals).toEqual(['fresh']);
  });

  it('reports a failure instead of showing a stale verdict as current', async () => {
    seedLive({ signal_at: true });
    const original = globalThis.fetch;
    globalThis.fetch = (async () => new Response(JSON.stringify({ detail: '配置不完整' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    })) as typeof fetch;

    try {
      await useAuditionStore.getState().run('token');
    } finally {
      globalThis.fetch = original;
    }

    const state = useAuditionStore.getState();
    expect(state.error).toContain('配置不完整');
    expect(state.loading).toBe(false);
  });

  it('asks nothing when the list is empty', async () => {
    seedLive({ signal_at: true });
    useAuditionStore.setState({ messages: [] });
    const spy = vi.fn();
    const original = globalThis.fetch;
    globalThis.fetch = (async () => { spy(); return new Response('{}'); }) as typeof fetch;

    try {
      await useAuditionStore.getState().run('token');
    } finally {
      globalThis.fetch = original;
    }

    expect(spy).not.toHaveBeenCalled();
    expect(useAuditionStore.getState().result).toBeNull();
  });

  it('names the group it is pretending to be', () => {
    expect(AUDITION_GROUP.alias).toBeTruthy();
    expect(toRequest([newMessage()], 1)[0].group_id).toBe(AUDITION_GROUP.groupId);
  });
});
