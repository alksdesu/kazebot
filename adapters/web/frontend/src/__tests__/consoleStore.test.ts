// QQ 控制台草稿态。两个键空间必须对得上：界面按 live_config 的键名读写，
// 写 yaml 时才用 /qq/state 公布的 paths 转成点分路径。
import { beforeEach, describe, expect, it } from 'vitest';

import type { QqInputRule } from '../api/supervisorClient';
import { barState } from '../console/components';
import {
  addWord,
  asWordList,
  effectiveValues,
  mergeDraft,
  readInputRule,
  readTristate,
  setByPath,
  useConsoleStore,
} from '../console/consoleStore';

const PATHS: Record<string, string> = {
  signal_at: 'trigger.signals.at',
  signal_name: 'trigger.signals.name',
  cooldown_group_sec: 'trigger.cooldown.per_group_sec',
  llm_intent_enabled: 'trigger.llm_intent.enabled',
  keyword_words: 'trigger.keyword.words',
};

// bot 公布的那份词表规则，逐字照抄 live_config 的 WORD_INPUT_RULE。
const WORD_RULE: QqInputRule = { kind: 'word', trim: true };

const dumpedWords = (words: string[]): string[] =>
  mergeDraft('', { keyword_words: words }, PATHS)
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.startsWith('- '))
    .map((line) => line.slice(2));

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

describe('setByPath', () => {
  it('creates the intermediate levels', () => {
    const doc: Record<string, unknown> = {};

    setByPath(doc, 'trigger.signals.at', true);

    expect(doc).toEqual({ trigger: { signals: { at: true } } });
  });

  it('keeps siblings while writing a leaf', () => {
    const doc: Record<string, unknown> = { trigger: { signals: { at: true }, group_mode: 'all' } };

    setByPath(doc, 'trigger.signals.name', true);

    expect(doc).toEqual({ trigger: { signals: { at: true, name: true }, group_mode: 'all' } });
  });

  it('deletes the key when the value is undefined', () => {
    // 删键 = 回落默认值，和写 false 是两种意思。
    const doc: Record<string, unknown> = { trigger: { signals: { at: false } } };

    setByPath(doc, 'trigger.signals.at', undefined);

    expect(doc).toEqual({ trigger: { signals: {} } });
  });

  it('replaces a non-object on the way down', () => {
    const doc: Record<string, unknown> = { trigger: 'nonsense' };

    setByPath(doc, 'trigger.signals.at', true);

    expect(doc).toEqual({ trigger: { signals: { at: true } } });
  });
});

describe('mergeDraft', () => {
  it('translates key names into the dotted yaml paths', () => {
    const out = mergeDraft('', { signal_name: true }, PATHS);

    expect(out).toContain('trigger:');
    expect(out).toContain('    name: true');
    // 键名本身不该出现在 yaml 里 —— 那是内部读取名，不是配置结构。
    expect(out).not.toContain('signal_name');
  });

  it('keeps everything the draft did not touch', () => {
    const raw = ['version: 1', 'channels:', '  allowed_groups:', '    - 123', ''].join('\n');

    const out = mergeDraft(raw, { signal_at: false }, PATHS);

    expect(out).toContain('version: 1');
    expect(out).toContain('- 123');
    expect(out).toContain('    at: false');
  });

  it('refuses a key the bot never published', () => {
    // 后端会用 400 拒绝无人消费的键，前端提前挡住能给出更准的话。
    expect(() => mergeDraft('', { made_up_key: 1 }, PATHS)).toThrow(/made_up_key/);
  });

  it('survives a corrupt base document', () => {
    // 磁盘上的 yaml 写坏了也必须能从控制台改回来。
    const out = mergeDraft('trigger:\n  signals:\n   at: [unclosed', { signal_at: true }, PATHS);

    expect(out).toContain('    at: true');
  });

  it('writes numbers as numbers', () => {
    const out = mergeDraft('', { cooldown_group_sec: 30 }, PATHS);

    expect(out).toContain('per_group_sec: 30');
    expect(out).not.toContain("per_group_sec: '30'");
  });

  it('quotes the words YAML 1.1 would read as booleans', () => {
    // bot 那边按 1.2 读，supervisor 和别的工具不一定 —— 裸写的 on 到消费方就成了 True。
    expect(dumpedWords(['on', 'off', 'yes', 'no', 'y', 'n', 'On', 'OFF'])).toEqual([
      '"on"', '"off"', '"yes"', '"no"', '"y"', '"n"', '"On"', '"OFF"',
    ]);
  });

  it('quotes a time-like word so it does not become a base-60 number', () => {
    // PyYAML 会把裸写的 12:30 解析成 750。
    expect(dumpedWords(['12:30'])).toEqual(['"12:30"']);
  });

  it('quotes the words every yaml reader would take for a scalar', () => {
    expect(dumpedWords(['true', 'false', 'null', '~', '30'])).toEqual([
      '"true"', '"false"', '"null"', '"~"', '"30"',
    ]);
  });

  it('leaves ordinary words unquoted so applying does not churn the file', () => {
    expect(dumpedWords(['早安', 'hello', '咪啪', 'gpt-4'])).toEqual([
      '早安', 'hello', '咪啪', 'gpt-4',
    ]);
  });
});

describe('readTristate', () => {
  beforeEach(() => {
    useConsoleStore.setState({ draft: {}, live: null });
  });

  it('reports null when the key is absent', () => {
    // null 表示「没配」，由 bot 按旧的 group_mode 推导；被压成 false 就等于悄悄接管。
    seedLive({});

    expect(readTristate(useConsoleStore.getState(), 'signal_at')).toBeNull();
  });

  it('reports null when the published value is null', () => {
    seedLive({ signal_at: null });

    expect(readTristate(useConsoleStore.getState(), 'signal_at')).toBeNull();
  });

  it('distinguishes an explicit false from an absent key', () => {
    seedLive({ signal_at: false });

    expect(readTristate(useConsoleStore.getState(), 'signal_at')).toBe(false);
  });

  it('reads true', () => {
    seedLive({ signal_at: true });

    expect(readTristate(useConsoleStore.getState(), 'signal_at')).toBe(true);
  });

  it('lets the draft win over the published value', () => {
    seedLive({ signal_at: true });
    useConsoleStore.getState().setDraft('signal_at', false);

    expect(readTristate(useConsoleStore.getState(), 'signal_at')).toBe(false);
  });
});

describe('effectiveValues', () => {
  beforeEach(() => {
    useConsoleStore.setState({ draft: {}, live: null });
  });

  it('lays the draft over the published values', () => {
    seedLive({ signal_at: true, signal_name: false });
    useConsoleStore.getState().setDraft('signal_name', true);

    expect(effectiveValues(useConsoleStore.getState())).toMatchObject({
      signal_at: true,
      signal_name: true,
    });
  });

  it('carries every published key, not just the edited ones', () => {
    // 按当前页过滤就等于试听和真实判定跑在两份配置上。
    seedLive({ signal_at: true, cooldown_group_sec: 30, name_words: ['咪啪'] });

    expect(Object.keys(effectiveValues(useConsoleStore.getState())).sort()).toEqual([
      'cooldown_group_sec', 'name_words', 'signal_at',
    ]);
  });

  it('keeps a tri-state null as null', () => {
    seedLive({ signal_at: null });

    expect(effectiveValues(useConsoleStore.getState())).toHaveProperty('signal_at', null);
  });

  it('is an empty object before the bot has published anything', () => {
    expect(effectiveValues(useConsoleStore.getState())).toEqual({});
  });
});

describe('addWord', () => {
  it('appends at the end so the configured order survives', () => {
    expect(addWord(['a', 'b'], 'c', WORD_RULE)).toEqual(['a', 'b', 'c']);
  });

  it('trims what was typed', () => {
    expect(addWord([], '  咪啪  ', WORD_RULE)).toEqual(['咪啪']);
  });

  it('keeps the whitespace when the bot says not to trim', () => {
    // 修剪与否也是 bot 说的：这里自己决定，就又多出一份规则。
    expect(addWord([], ' 咪啪 ', { kind: 'word', trim: false })).toEqual([' 咪啪 ']);
  });

  it('returns the same list for a duplicate so no draft entry is created', () => {
    // 后端会去重，前端放进去的话保存后重复项消失，看起来像界面把输入吞了。
    const words = ['咪啪'];

    expect(addWord(words, '咪啪', WORD_RULE)).toBe(words);
  });

  it('returns the same list for blank input', () => {
    const words = ['咪啪'];

    expect(addWord(words, '   ', WORD_RULE)).toBe(words);
  });
});

describe('asWordList', () => {
  it('keeps a list of strings as the very same array', () => {
    // 换个引用就够让 useSyncExternalStore 以为 store 变了，然后一直空转。
    const words = ['咪啪', '早安'];

    expect(asWordList(words)).toBe(words);
  });

  it('drops items that are not strings', () => {
    // yaml 里裸写的 true 后端已经丢了；漏进来的话是个看不见的空 chip，
    // 删掉别的词就把它原样写回 yaml。
    expect(asWordList([true, '早安', null, 42])).toEqual(['早安']);
  });

  it('returns the same filtered array for the same source', () => {
    const words = [true, '早安'];

    expect(asWordList(words)).toBe(asWordList(words));
  });

  it('falls back to one shared empty list', () => {
    expect(asWordList(undefined)).toBe(asWordList('not a list'));
  });
});

describe('useInputRule', () => {
  beforeEach(() => {
    useConsoleStore.setState({ draft: {}, live: null });
  });

  it('reads the rule the bot published for that key', () => {
    useConsoleStore.setState({
      live: {
        published: true,
        applied: true,
        file: { exists: true, mtime_ns: 1, size: 1 },
        state: { values: {}, paths: PATHS, input_rules: { keyword_words: WORD_RULE } },
      },
    });

    expect(readInputRule(useConsoleStore.getState(), 'keyword_words')).toEqual(WORD_RULE);
  });

  it('is null for a key the bot published no rule for', () => {
    expect(readInputRule(useConsoleStore.getState(), 'keyword_words')).toBeNull();
  });
});

describe('the draft counter', () => {
  beforeEach(() => {
    useConsoleStore.setState({ draft: {}, live: null, raw: '', notice: '', error: '' });
  });

  it('counts a change away from the applied value', () => {
    seedLive({ signal_name: false });

    useConsoleStore.getState().setDraft('signal_name', true);

    expect(Object.keys(useConsoleStore.getState().draft)).toEqual(['signal_name']);
  });

  it('drops the entry when the value returns to what is applied', () => {
    // 否则「3 项待应用」里会混进已经被改回来的项。
    seedLive({ signal_name: false });

    useConsoleStore.getState().setDraft('signal_name', true);
    useConsoleStore.getState().setDraft('signal_name', false);

    expect(useConsoleStore.getState().draft).toEqual({});
  });

  it('compares structurally, not by reference', () => {
    seedLive({ name_words: ['咪啪'] });

    useConsoleStore.getState().setDraft('name_words', ['咪啪']);

    expect(useConsoleStore.getState().draft).toEqual({});
  });

  it('treats a tri-state null as different from false', () => {
    // signals.at 留空表示跟随旧的 group_mode，显式 false 是接管并关掉。
    seedLive({ signal_at: null });

    useConsoleStore.getState().setDraft('signal_at', false);

    expect(useConsoleStore.getState().draft).toEqual({ signal_at: false });
  });

  it('discard clears everything staged', () => {
    seedLive({ signal_name: false });
    useConsoleStore.getState().setDraft('signal_name', true);

    useConsoleStore.getState().discard();

    expect(useConsoleStore.getState().draft).toEqual({});
  });

  it('apply is a no-op with nothing staged', async () => {
    seedLive({ signal_name: false });
    let fetched = false;
    const original = globalThis.fetch;
    globalThis.fetch = (async () => {
      fetched = true;
      return new Response('{}');
    }) as typeof fetch;

    try {
      await useConsoleStore.getState().apply('token');
    } finally {
      globalThis.fetch = original;
    }

    expect(fetched).toBe(false);
  });
});

describe('apply', () => {
  beforeEach(() => {
    useConsoleStore.setState({ draft: {}, live: null, raw: '', notice: '', error: '' });
  });

  it('sends the merged document and clears the draft', async () => {
    seedLive({ signal_name: false });
    useConsoleStore.setState({ raw: 'version: 1\n' });
    useConsoleStore.getState().setDraft('signal_name', true);
    const sent: string[] = [];
    const original = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'PUT') {
        sent.push(String(JSON.parse(String(init.body)).content));
        return new Response(JSON.stringify({ ok: true, warnings: [] }), {
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/qq/state')) {
        return new Response(JSON.stringify({ published: true, applied: true, file: {}, state: { values: { signal_name: true }, paths: PATHS } }), {
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ content: '', exists: false }), {
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof fetch;

    try {
      await useConsoleStore.getState().apply('token');
    } finally {
      globalThis.fetch = original;
    }

    expect(sent).toHaveLength(1);
    expect(sent[0]).toContain('version: 1');
    expect(sent[0]).toContain('    name: true');
    expect(useConsoleStore.getState().draft).toEqual({});
  });

  it('keeps the draft when the save fails', async () => {
    // 丢掉草稿等于把运营者刚做的改动静默吞掉。
    seedLive({ signal_name: false });
    useConsoleStore.getState().setDraft('signal_name', true);
    const original = globalThis.fetch;
    globalThis.fetch = (async () => new Response(JSON.stringify({ detail: '无人消费的配置项' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    })) as typeof fetch;

    try {
      await useConsoleStore.getState().apply('token');
    } finally {
      globalThis.fetch = original;
    }

    const state = useConsoleStore.getState();
    expect(state.draft).toEqual({ signal_name: true });
    expect(state.error).toContain('无人消费的配置项');
    expect(state.saving).toBe(false);
  });
});

// 顶部那条状态条。它是「bot 有没有吃到这份配置」的唯一显示，报错了等于没有。
describe('生效状态条', () => {
  const live = (over: Record<string, unknown> = {}) =>
    ({ published: true, stale: false, applied: true, ...over }) as Parameters<typeof barState>[0];

  it('bot 认账了才是已生效', () => {
    expect(barState(live(), '')).toEqual({ tone: 'live', text: '已生效' });
  });

  it('心跳断了不能显示成已生效', () => {
    expect(barState(live({ stale: true }), '').tone).toBe('halt');
  });

  it('bot 还在用旧配置也是红的', () => {
    expect(barState(live({ applied: false }), '').tone).toBe('halt');
  });

  // 语法错误压过一切：文件写坏时 applied 仍然为真（bot 用的是上一份），
  // 按 applied 判就会显示「已生效」，而实际上刚才那次改动一个字都没进去。
  it('语法错误压过 applied', () => {
    expect(barState(live(), 'line 3: bad indent')).toEqual({
      tone: 'halt', text: '配置有语法错误，bot 仍在用上一份',
    });
  });

  it('bot 没上报时是灰的，不是红的', () => {
    expect(barState(null, '')).toEqual({ tone: 'idle', text: '等待 bot 上报' });
  });
});
