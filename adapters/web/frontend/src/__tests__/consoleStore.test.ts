// 控制台草稿态。两个键空间必须对得上：界面按 live_config 的键名读写，
// 写 yaml 时才用 state 接口公布的 paths 转成点分路径。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConfigInputRule } from '../api/supervisorClient';
import { FEATURES_SCOPE } from '../console/configScope';
import { barState } from '../console/PlatformPage';
import {
  addWord,
  asWordList,
  configStore,
  countDraftFor,
  effectiveValues,
  mergeDraft,
  platformConsoleStore,
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
const WORD_RULE: ConfigInputRule = { kind: 'word', trim: true };

const dumpedWords = (words: string[]): string[] =>
  mergeDraft('', { keyword_words: words }, PATHS)
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.startsWith('- '))
    .map((line) => line.slice(2));

const seedLive = (values: Record<string, unknown>) => {
  platformConsoleStore('qq').setState({
    live: {
      published: true,
      applied: true,
      file: { exists: true, mtime_ns: 1, size: 1 },
      state: { values, paths: PATHS },
    },
    draft: {},
  });
};

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

/** 假的配置接口：记下每次 PUT 的原文，读原文时还回最近写进去的那份，状态按 applied 回。 */
const serveConfig = (values: Record<string, unknown>, { applied = true, raw = 'version: 1\n' } = {}): string[] => {
  const sent: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      sent.push(String(JSON.parse(String(init.body)).content));
      return json({ ok: true, warnings: [] });
    }
    if (String(input).endsWith('/config/state')) {
      return json({ published: true, applied, file: {}, state: { values, paths: PATHS }, platforms: [] });
    }
    return json({ content: sent.length ? sent[sent.length - 1] : raw, exists: true });
  }));
  return sent;
};

afterEach(() => { vi.unstubAllGlobals(); });

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

  it('deletes the key when the value is undefined and folds the sections it emptied', () => {
    // 删键 = 交给下一层，和写 false 是两种意思。键路径互不嵌套，删空的段只是容器，收掉不改任何值。
    const doc: Record<string, unknown> = { trigger: { signals: { at: false } } };

    setByPath(doc, 'trigger.signals.at', undefined);

    expect(doc).toEqual({});
  });

  it('stops folding at the first section that still holds something', () => {
    const doc: Record<string, unknown> = { version: 1, trigger: { signals: { at: false }, group_mode: 'all' } };

    setByPath(doc, 'trigger.signals.at', undefined);

    expect(doc).toEqual({ version: 1, trigger: { group_mode: 'all' } });
  });

  it('deleting a key the file never had changes nothing', () => {
    const doc: Record<string, unknown> = { trigger: 'nonsense', channels: {} };

    setByPath(doc, 'trigger.signals.at', undefined);
    setByPath(doc, 'channels.allowed_groups', undefined);

    expect(doc).toEqual({ trigger: 'nonsense', channels: {} });
  });

  it('refuses to overwrite a non-object on the way down', () => {
    const doc: Record<string, unknown> = { trigger: 'nonsense' };

    expect(() => setByPath(doc, 'trigger.signals.at', true)).toThrow();
    expect(doc).toEqual({ trigger: 'nonsense' });
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

  it('rejects a corrupt base document without overwriting it', () => {
    expect(() => mergeDraft('broken: [', { signal_at: true }, PATHS)).toThrow();
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

  it('a null deletes the line so the next layer takes over', () => {
    const raw = ['version: 1', 'trigger:', '  signals:', '    at: false', '    name: true', ''].join('\n');

    const out = mergeDraft(raw, { signal_at: null }, PATHS);

    expect(out).not.toContain('at:');
    expect(out).toContain('    name: true');
  });
});

describe('readTristate', () => {
  beforeEach(() => {
    platformConsoleStore('qq').setState({ draft: {}, live: null });
  });

  it('reports null when the key is absent', () => {
    // null 表示「没配」，由 bot 按旧的 group_mode 推导；被压成 false 就等于悄悄接管。
    seedLive({});

    expect(readTristate(platformConsoleStore('qq').getState(), 'signal_at')).toBeNull();
  });

  it('reports null when the published value is null', () => {
    seedLive({ signal_at: null });

    expect(readTristate(platformConsoleStore('qq').getState(), 'signal_at')).toBeNull();
  });

  it('distinguishes an explicit false from an absent key', () => {
    seedLive({ signal_at: false });

    expect(readTristate(platformConsoleStore('qq').getState(), 'signal_at')).toBe(false);
  });

  it('reads true', () => {
    seedLive({ signal_at: true });

    expect(readTristate(platformConsoleStore('qq').getState(), 'signal_at')).toBe(true);
  });

  it('lets the draft win over the published value', () => {
    seedLive({ signal_at: true });
    platformConsoleStore('qq').getState().setDraft('signal_at', false);

    expect(readTristate(platformConsoleStore('qq').getState(), 'signal_at')).toBe(false);
  });
});

describe('effectiveValues', () => {
  beforeEach(() => {
    platformConsoleStore('qq').setState({ draft: {}, live: null });
  });

  it('lays the draft over the published values', () => {
    seedLive({ signal_at: true, signal_name: false });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);

    expect(effectiveValues(platformConsoleStore('qq').getState())).toMatchObject({
      signal_at: true,
      signal_name: true,
    });
  });

  it('carries every published key, not just the edited ones', () => {
    // 按当前页过滤就等于试听和真实判定跑在两份配置上。
    seedLive({ signal_at: true, cooldown_group_sec: 30, name_words: ['咪啪'] });

    expect(Object.keys(effectiveValues(platformConsoleStore('qq').getState())).sort()).toEqual([
      'cooldown_group_sec', 'name_words', 'signal_at',
    ]);
  });

  it('keeps a tri-state null as null', () => {
    seedLive({ signal_at: null });

    expect(effectiveValues(platformConsoleStore('qq').getState())).toHaveProperty('signal_at', null);
  });

  it('is an empty object before the bot has published anything', () => {
    expect(effectiveValues(platformConsoleStore('qq').getState())).toEqual({});
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
    platformConsoleStore('qq').setState({ draft: {}, live: null });
  });

  it('reads the rule the bot published for that key', () => {
    platformConsoleStore('qq').setState({
      live: {
        published: true,
        applied: true,
        file: { exists: true, mtime_ns: 1, size: 1 },
        state: { values: {}, paths: PATHS, input_rules: { keyword_words: WORD_RULE } },
      },
    });

    expect(readInputRule(platformConsoleStore('qq').getState(), 'keyword_words')).toEqual(WORD_RULE);
  });

  it('is null for a key the bot published no rule for', () => {
    expect(readInputRule(platformConsoleStore('qq').getState(), 'keyword_words')).toBeNull();
  });
});

describe('the draft counter', () => {
  beforeEach(() => {
    platformConsoleStore('qq').setState({ draft: {}, live: null, raw: '', notice: '', error: '' });
  });

  it('counts a change away from the applied value', () => {
    seedLive({ signal_name: false });

    platformConsoleStore('qq').getState().setDraft('signal_name', true);

    expect(Object.keys(platformConsoleStore('qq').getState().draft)).toEqual(['signal_name']);
  });

  it('drops the entry when the value returns to what is applied', () => {
    // 否则「3 项待应用」里会混进已经被改回来的项。
    seedLive({ signal_name: false });

    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    platformConsoleStore('qq').getState().setDraft('signal_name', false);

    expect(platformConsoleStore('qq').getState().draft).toEqual({});
  });

  it('compares structurally, not by reference', () => {
    seedLive({ name_words: ['咪啪'] });

    platformConsoleStore('qq').getState().setDraft('name_words', ['咪啪']);

    expect(platformConsoleStore('qq').getState().draft).toEqual({});
  });

  it('treats a tri-state null as different from false', () => {
    // signals.at 留空表示跟随旧的 group_mode，显式 false 是接管并关掉。
    seedLive({ signal_at: null });

    platformConsoleStore('qq').getState().setDraft('signal_at', false);

    expect(platformConsoleStore('qq').getState().draft).toEqual({ signal_at: false });
  });

  it('discard clears everything staged', () => {
    seedLive({ signal_name: false });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);

    platformConsoleStore('qq').getState().discard();

    expect(platformConsoleStore('qq').getState().draft).toEqual({});
  });

  it('discard with names drops only those keys', () => {
    seedLive({ signal_name: false, signal_at: true });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    platformConsoleStore('qq').getState().setDraft('signal_at', false);

    platformConsoleStore('qq').getState().discard(['signal_at']);

    expect(platformConsoleStore('qq').getState().draft).toEqual({ signal_name: true });
  });

  it('counts only the staged keys among those a block owns', () => {
    seedLive({ signal_name: false, signal_at: true });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    platformConsoleStore('qq').getState().setDraft('signal_at', false);

    const state = platformConsoleStore('qq').getState();
    expect(countDraftFor(state, ['signal_at', 'cooldown_group_sec'])).toBe(1);
    expect(countDraftFor(state, ['cooldown_group_sec'])).toBe(0);
  });

  it('apply is a no-op with nothing staged', async () => {
    seedLive({ signal_name: false });
    serveConfig({});

    await platformConsoleStore('qq').getState().apply('token');

    expect(fetch).not.toHaveBeenCalled();
  });
});

describe('apply', () => {
  beforeEach(() => {
    platformConsoleStore('qq').setState({ draft: {}, live: null, raw: '', notice: '', error: '' });
  });

  it('sends the merged document, clears the draft and waits for the bot to confirm', async () => {
    seedLive({ signal_name: false });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    const sent = serveConfig({ signal_name: true });

    await platformConsoleStore('qq').getState().apply('token');

    expect(sent).toHaveLength(1);
    expect(sent[0]).toContain('version: 1');
    expect(sent[0]).toContain('    name: true');
    const state = platformConsoleStore('qq').getState();
    expect(state.draft).toEqual({});
    // bot 第一轮就认账，不该冒出「没在 3 秒内确认」。
    expect(state.notice).toBe('');
    expect(state.live?.applied).toBe(true);
  });

  it('keeps the draft when the save fails', async () => {
    // 丢掉草稿等于把运营者刚做的改动静默吞掉。
    seedLive({ signal_name: false });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: '无人消费的配置项' }, 400)));

    await platformConsoleStore('qq').getState().apply('token');

    const state = platformConsoleStore('qq').getState();
    expect(state.draft).toEqual({ signal_name: true });
    expect(state.error).toContain('无人消费的配置项');
    expect(state.saving).toBe(false);
  });
});

describe('save', () => {
  beforeEach(() => {
    platformConsoleStore('qq').setState({ draft: {}, live: null, raw: '', notice: '', error: '' });
    configStore(FEATURES_SCOPE).setState({ draft: {}, live: null, raw: '', notice: '', error: '' });
  });

  it('writes only the keys it was given and leaves the rest staged', async () => {
    seedLive({ signal_name: false, signal_at: true });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    platformConsoleStore('qq').getState().setDraft('signal_at', false);
    const sent = serveConfig({ signal_name: true, signal_at: true });

    const outcome = await platformConsoleStore('qq').getState().save('token', ['signal_name']);

    expect(outcome).toEqual({ ok: true, message: '' });
    expect(sent[0]).toContain('    name: true');
    expect(sent[0]).not.toContain('at:');
    const state = platformConsoleStore('qq').getState();
    expect(state.draft).toEqual({ signal_at: false });
    // 结果留给区块自己显示，页面级的提示不动。
    expect(state.notice).toBe('');
    expect(state.error).toBe('');
  });

  it('does not touch the server when none of its keys is staged', async () => {
    seedLive({ signal_name: false });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    serveConfig({});

    expect(await platformConsoleStore('qq').getState().save('token', ['signal_at'])).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('hands a failure back to the caller and keeps the draft', async () => {
    seedLive({ signal_name: false });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: '只能写在平台配置里：trigger.signals.name' }, 400)));

    const outcome = await platformConsoleStore('qq').getState().save('token', ['signal_name']);

    expect(outcome?.ok).toBe(false);
    expect(outcome?.message).toContain('只能写在平台配置里');
    expect(platformConsoleStore('qq').getState().draft).toEqual({ signal_name: true });
    expect(platformConsoleStore('qq').getState().error).toBe('');
  });

  it('the global file goes through /features and reports in its own words', async () => {
    configStore(FEATURES_SCOPE).setState({
      live: { published: true, applied: true, file: { exists: true, mtime_ns: 1, size: 1 }, state: { values: { signal_name: false }, paths: PATHS } },
      draft: {},
    });
    configStore(FEATURES_SCOPE).getState().setDraft('signal_name', true);
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(`${init?.method || 'GET'} ${String(input)}`);
      if (init?.method === 'PUT') return json({ ok: true, warnings: [] });
      if (String(input).endsWith('/config/state')) {
        return json({ published: true, applied: true, file: {}, state: { values: {}, paths: PATHS }, platforms: [] });
      }
      // 别人刚改过全局文件：写回之后读到的不是自己写的那份。
      return json({ content: 'version: 2\n', exists: true });
    }));

    const outcome = await configStore(FEATURES_SCOPE).getState().save('token', ['signal_name']);

    expect(outcome).toEqual({ ok: true, message: '全局文件在保存之后又被改过，请刷新核对当前内容。' });
    expect(requests.some((line) => line.startsWith('PUT ') && line.endsWith('/features/config/raw'))).toBe(true);
    expect(requests.filter((line) => line.includes('/platforms/'))).toEqual([]);
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
