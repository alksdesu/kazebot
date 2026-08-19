// 试听的消息编排与请求发送。判定本身在后端，这里只负责「拿什么去问」和「问的次序」。
import { create } from 'zustand';

import {
  dryRunQqTrigger,
  type QqDryRunMessage,
  type QqDryRunResult,
  type QqDryRunVerdict,
} from '../api/supervisorClient';
import { effectiveValues, useConsoleStore } from './consoleStore';

/** 两个发送者，因为「每人冷却」按 (群, 人) 分组 —— 只有一个人时它和「每群冷却」看不出区别。 */
export const SENDERS = {
  a: { userId: 900_000_001, alias: 'User_a3f2' },
  b: { userId: 900_000_002, alias: 'User_b901' },
} as const;

export type SenderKey = keyof typeof SENDERS;

export const AUDITION_GROUP = { groupId: 700_000_001, alias: 'Group_7c1' } as const;

/** 后端一次收 200 条，界面上远够不到；这个上限只是防止手滑粘一整份聊天记录进来。 */
export const MAX_MESSAGES = 30;

export interface AuditionMessage {
  id: string;
  text: string;
  sender: SenderKey;
  atMe: boolean;
  replyToBot: boolean;
}

let idSeed = 0;
const nextId = (): string => `m${++idSeed}`;

export function newMessage(overrides: Partial<Omit<AuditionMessage, 'id'>> = {}): AuditionMessage {
  return { id: nextId(), text: '', sender: 'a', atMe: false, replyToBot: false, ...overrides };
}

/** 预置样例覆盖「命中名字 / 命中 @ / 什么都不命中 / 命中被回复」四种结局。 */
export function defaultMessages(): AuditionMessage[] {
  return [
    newMessage({ text: '咪啪在吗' }),
    newMessage({ text: '今天天气不错', sender: 'b' }),
    newMessage({ text: '@咪啪 帮我查下天气', sender: 'b', atMe: true }),
    newMessage({ text: '这个说得对', replyToBot: true }),
  ];
}

/** 摊成 dry-run 的入参。at_sec 显式给：默认「每条隔一秒依次到达」看不出长冷却窗口的效果。 */
export function toRequest(messages: AuditionMessage[], intervalSec: number): QqDryRunMessage[] {
  const step = Number.isFinite(intervalSec) && intervalSec > 0 ? intervalSec : 0;
  return messages.map((message, index) => ({
    text: message.text,
    group_id: AUDITION_GROUP.groupId,
    user_id: SENDERS[message.sender].userId,
    at_me: message.atMe,
    reply_to_bot: message.replyToBot,
    at_sec: index * step,
  }));
}

/** 判定按消息 id 归位。对不上的消息没有判定可显示，好过显示上一轮别人的结果。 */
export function alignVerdicts(
  resultIds: string[],
  verdicts: QqDryRunVerdict[] | undefined,
): Map<string, QqDryRunVerdict> {
  const out = new Map<string, QqDryRunVerdict>();
  (verdicts || []).forEach((verdict) => {
    const id = resultIds[verdict.index];
    if (id !== undefined) out.set(id, verdict);
  });
  return out;
}

interface AuditionState {
  messages: AuditionMessage[];
  intervalSec: number;
  cooldown: boolean;
  result: QqDryRunResult | null;
  /** 这份结果是问哪几条消息得来的。删掉一条之后 index 会整体错位，
   *  按 id 对齐才不会把判定挂到别人身上。 */
  resultIds: string[];
  loading: boolean;
  error: string;

  patch: (id: string, changes: Partial<Omit<AuditionMessage, 'id'>>) => void;
  explode: (id: string, lines: string[]) => void;
  add: () => void;
  remove: (id: string) => void;
  reset: () => void;
  setInterval: (seconds: number) => void;
  setCooldown: (on: boolean) => void;
  schedule: (token: string) => void;
  run: (token: string) => Promise<void>;
}

// 只接受最新一次请求的回包。debounce 挡不住乱序：先发的请求可能后到，
// 那时界面显示的是上一份配置的判定，而运营者以为看的是刚改完的。
let issued = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
const DEBOUNCE_MS = 220;

export const useAuditionStore = create<AuditionState>((set, get) => ({
  messages: defaultMessages(),
  intervalSec: 1,
  cooldown: true,
  result: null,
  resultIds: [],
  loading: false,
  error: '',

  patch: (id, changes) => set((state) => ({
    messages: state.messages.map((m) => (m.id === id ? { ...m, ...changes } : m)),
  })),

  // 从群里复制出来的一段就是多行文本。逐行贴进去太难用，所以粘贴时就地铺开成多条。
  explode: (id, lines) => set((state) => {
    const at = state.messages.findIndex((m) => m.id === id);
    if (at < 0 || !lines.length) return state;
    const target = state.messages[at];
    const room = MAX_MESSAGES - (state.messages.length - 1);
    const grown = lines.slice(0, Math.max(1, room)).map((text, offset) => (
      offset === 0 ? { ...target, text } : newMessage({ text, sender: target.sender })
    ));
    return { messages: [...state.messages.slice(0, at), ...grown, ...state.messages.slice(at + 1)] };
  }),

  add: () => set((state) => (
    state.messages.length >= MAX_MESSAGES ? state : { messages: [...state.messages, newMessage()] }
  )),

  remove: (id) => set((state) => ({ messages: state.messages.filter((m) => m.id !== id) })),

  reset: () => set({ messages: defaultMessages(), error: '' }),

  setInterval: (seconds) => set({ intervalSec: Number.isFinite(seconds) ? Math.max(0, seconds) : 0 }),

  setCooldown: (on) => set({ cooldown: on }),

  schedule: (token) => {
    if (timer !== undefined) clearTimeout(timer);
    timer = setTimeout(() => { timer = undefined; void get().run(token); }, DEBOUNCE_MS);
  },

  run: async (token) => {
    const { messages, intervalSec, cooldown } = get();
    if (!messages.length) {
      set({ result: null, resultIds: [], error: '', loading: false });
      return;
    }
    const mine = ++issued;
    const asked = messages.map((m) => m.id);
    set({ loading: true, error: '' });
    try {
      const result = await dryRunQqTrigger(token, toRequest(messages, intervalSec), {
        config: effectiveValues(useConsoleStore.getState()),
        cooldown,
      });
      if (mine !== issued) return;
      set({ result, resultIds: asked, loading: false });
    } catch (error) {
      if (mine !== issued) return;
      set({ loading: false, error: error instanceof Error ? error.message : '试听失败' });
    }
  },
}));
