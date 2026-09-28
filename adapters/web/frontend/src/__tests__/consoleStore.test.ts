// 控制台草稿态。两个键空间必须对得上：界面按 live_config 的键名读写，
// 写 yaml 时才用 state 接口公布的 paths 转成点分路径。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConfigInputRule, ConfigSource } from '../api/supervisorClient';
import { FEATURES_SCOPE, platformScope, type ConfigScope } from '../console/configScope';
import { barState } from '../console/PlatformPage';
import {
  addWord,
  asWordList,
  configStore,
  confirmationNote,
  countDraftFor,
  displayedValue,
  effectiveValues,
  mergeDraft,
  platformConsoleStore,
  readInputRule,
  readTristate,
  setByPath,
  type ConfigConsoleStore,
} from '../console/consoleStore';
import { BLOCK_COMMENTS_DROPPED, WHOLE_FILE_REWRITTEN } from '../console/yamlRewrite';
import { adoptPlatforms, QQ_NAPCAT, resetPlatforms, type PlatformDescriptor } from '../platforms/registry';

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
  mergeDraft('', { keyword_words: words }, PATHS).content
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

/** 带来源的生效快照。不给 sources 就是不公布来源的旧版生效方。 */
const seedSourced = (
  store: ConfigConsoleStore,
  values: Record<string, unknown>,
  sources?: Record<string, ConfigSource>,
) => {
  store.setState({
    live: {
      published: true,
      applied: true,
      file: { exists: true, mtime_ns: 1, size: 1 },
      state: { values, paths: PATHS, ...(sources ? { sources } : {}) },
    },
    draft: {},
  });
};

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

/** 假的配置接口：记下每次 PUT 的原文，读原文时还回最近写进去的那份，状态按 applied 回。
 *  返回的盒子随时可以改口，认账那边隔一会儿探一次，中途换答案才测得到各种结论。 */
const serveConfig = (values: Record<string, unknown>, { applied = true, raw = 'version: 1\n' } = {}) => {
  const box = {
    sent: [] as string[],
    applied,
    /** 非空表示文件被别处改过：读原文不再还回自己写的那份。 */
    override: '',
    /** 只让状态端点失败：写回用的读原文还得能走通。 */
    stateDown: false,
    /** 每次状态快照换个心跳读数，看得出 live 有没有跟着换。 */
    beat: 0,
    warnings: [] as string[],
  };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      box.sent.push(String(JSON.parse(String(init.body)).content));
      return json({ ok: true, warnings: box.warnings });
    }
    if (String(input).endsWith('/config/state')) {
      if (box.stateDown) throw new Error('读不到 bot 的状态');
      box.beat += 1;
      return json({
        published: true, applied: box.applied, age_sec: box.beat, file: {},
        state: { values, paths: PATHS }, platforms: [],
      });
    }
    return json({ content: box.override || (box.sent.length ? box.sent[box.sent.length - 1] : raw), exists: true });
  }));
  return box;
};

/** 写回之后 bot 还没认账时，store 的那份快照：值是写进去的，applied 仍为假。 */
const stage = (store: ConfigConsoleStore, values: Record<string, unknown>) => {
  store.setState({
    live: {
      published: true,
      applied: false,
      file: { exists: true, mtime_ns: 1, size: 1 },
      state: { values, paths: PATHS },
    },
    draft: {},
    raw: '',
    saving: false,
    notice: '',
    loadError: '',
    applyError: '',
    confirmation: null,
  });
};

// 快等 12 轮 × 250ms，之后后台每 2000ms 再探，从写完算满 60 秒才算没确认。
const QUICK_MS = 250 * 12;
const CONFIRM_MS = 61_000;

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
    const { content: out } = mergeDraft('', { signal_name: true }, PATHS);

    expect(out).toContain('trigger:');
    expect(out).toContain('    name: true');
    // 键名本身不该出现在 yaml 里 —— 那是内部读取名，不是配置结构。
    expect(out).not.toContain('signal_name');
  });

  it('keeps everything the draft did not touch', () => {
    const raw = ['version: 1', 'channels:', '  allowed_groups:', '    - 123', ''].join('\n');

    const { content: out } = mergeDraft(raw, { signal_at: false }, PATHS);

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
    const { content: out } = mergeDraft('', { cooldown_group_sec: 30 }, PATHS);

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

    const { content: out } = mergeDraft(raw, { signal_at: null }, PATHS);

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

  it('resolves a key staged for deletion to the next layer', () => {
    // 试听要按写回之后真正会跑的值判：平台文件删掉这一行，落到的是全局那份。
    seedSourced(platformConsoleStore('qq'), { signal_name: true, signal_at: true }, { signal_name: 'platform', signal_at: 'platform' });
    platformConsoleStore('qq').getState().inherit('signal_name');

    expect(effectiveValues(platformConsoleStore('qq').getState(), { signal_name: false })).toMatchObject({
      signal_name: false,
      signal_at: true,
    });
  });

  it('keeps the running value for a deleted key until the next layer arrives', () => {
    seedSourced(platformConsoleStore('qq'), { signal_name: true }, { signal_name: 'platform' });
    platformConsoleStore('qq').getState().inherit('signal_name');

    expect(effectiveValues(platformConsoleStore('qq').getState())).toHaveProperty('signal_name', true);
  });
});

describe('displayedValue', () => {
  beforeEach(() => {
    platformConsoleStore('qq').setState({ draft: {}, live: null });
  });

  it('shows the next layer for a key staged for deletion', () => {
    seedSourced(platformConsoleStore('qq'), { signal_name: true }, { signal_name: 'platform' });
    platformConsoleStore('qq').getState().inherit('signal_name');
    const state = platformConsoleStore('qq').getState();

    expect(displayedValue(state, 'signal_name', { signal_name: false })).toBe(false);
    expect(readTristate(state, 'signal_name', { signal_name: false })).toBe(false);
  });

  it('leaves a deleted key without a value until the next layer arrives', () => {
    // 交给控件自己的 fallback。拿现值顶上的话，选了「跟随全局」看着像没反应。
    seedSourced(platformConsoleStore('qq'), { signal_name: true }, { signal_name: 'platform' });
    platformConsoleStore('qq').getState().inherit('signal_name');

    expect(displayedValue(platformConsoleStore('qq').getState(), 'signal_name')).toBeUndefined();
  });

  it('ignores the next layer while nothing is staged', () => {
    seedSourced(platformConsoleStore('qq'), { signal_name: true }, { signal_name: 'platform' });

    expect(displayedValue(platformConsoleStore('qq').getState(), 'signal_name', { signal_name: false })).toBe(true);
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
    platformConsoleStore('qq').setState({ draft: {}, live: null, raw: '', notice: '', loadError: '', applyError: '', confirmation: null });
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

describe('inherit and pin', () => {
  const qq = () => platformConsoleStore('qq');
  const global = () => configStore(FEATURES_SCOPE);

  beforeEach(() => {
    qq().setState({ draft: {}, live: null, notice: '' });
    global().setState({ draft: {}, live: null, notice: '' });
  });

  it('inherit stages a line deletion for a key the platform file sets', () => {
    seedSourced(qq(), { cooldown_group_sec: 30 }, { cooldown_group_sec: 'platform' });

    qq().getState().inherit('cooldown_group_sec');

    expect(qq().getState().draft).toEqual({ cooldown_group_sec: null });
  });

  it('inherit on a key already following the global file only drops the pending edit', () => {
    // 文件里本来就没这一行：记成删行什么也不改，头上却多挂一个「1 项待应用」。
    seedSourced(qq(), { cooldown_group_sec: 30 }, { cooldown_group_sec: 'global' });
    qq().getState().setDraft('cooldown_group_sec', 60);

    qq().getState().inherit('cooldown_group_sec');

    expect(qq().getState().draft).toEqual({});
  });

  it('inherit never stages a deletion when the bot does not publish sources', () => {
    // 看不出这一行在不在文件里，删错了就是悄悄丢掉运营者写的值。
    seedSourced(qq(), { cooldown_group_sec: 30 });

    qq().getState().inherit('cooldown_group_sec');

    expect(qq().getState().draft).toEqual({});
  });

  it('pin stages the running value even when it equals what is applied', () => {
    // 按值比较会当成没改。写进平台文件之后全局再怎么改这个平台都不跟，这正是选「本平台」的意思。
    seedSourced(qq(), { cooldown_group_sec: 30 }, { cooldown_group_sec: 'global' });

    qq().getState().pin('cooldown_group_sec');

    expect(qq().getState().draft).toEqual({ cooldown_group_sec: 30 });
  });

  it('pin writes the env fallback into the file', () => {
    seedSourced(qq(), { cooldown_group_sec: 12 }, { cooldown_group_sec: 'env' });

    qq().getState().pin('cooldown_group_sec');

    expect(qq().getState().draft).toEqual({ cooldown_group_sec: 12 });
  });

  it('pin keeps an edit already in progress', () => {
    seedSourced(qq(), { cooldown_group_sec: 30 }, { cooldown_group_sec: 'global' });
    qq().getState().setDraft('cooldown_group_sec', 60);

    qq().getState().pin('cooldown_group_sec');

    expect(qq().getState().draft).toEqual({ cooldown_group_sec: 60 });
  });

  it('pin after inherit on a key the file sets just takes the deletion back', () => {
    seedSourced(qq(), { cooldown_group_sec: 30 }, { cooldown_group_sec: 'platform' });
    qq().getState().inherit('cooldown_group_sec');

    qq().getState().pin('cooldown_group_sec');

    expect(qq().getState().draft).toEqual({});
  });

  it('pin swaps a stale deletion for the running value once the line is gone', () => {
    // 删行草稿还挂着，这一行却已经在别处删掉了：再钉得写回现值，否则写回去什么也不留。
    seedSourced(qq(), { cooldown_group_sec: 30 }, { cooldown_group_sec: 'platform' });
    qq().getState().inherit('cooldown_group_sec');
    const { draft } = qq().getState();
    seedSourced(qq(), { cooldown_group_sec: 45 }, { cooldown_group_sec: 'global' });
    qq().setState({ draft });

    qq().getState().pin('cooldown_group_sec');

    expect(qq().getState().draft).toEqual({ cooldown_group_sec: 45 });
  });

  it('pin does nothing for a key whose value is not published', () => {
    // undefined 进了草稿，写回时就成了删行。
    seedSourced(qq(), {}, {});

    qq().getState().pin('cooldown_group_sec');

    expect(qq().getState().draft).toEqual({});
  });

  it('editing back to the applied value after a pin counts as undoing it', () => {
    // 隐式改值一律按值比较；要钉同样的值得再点一次「本平台」。
    seedSourced(qq(), { cooldown_group_sec: 30 }, { cooldown_group_sec: 'global' });
    qq().getState().pin('cooldown_group_sec');
    qq().getState().setDraft('cooldown_group_sec', 60);

    qq().getState().setDraft('cooldown_group_sec', 30);

    expect(qq().getState().draft).toEqual({});
  });

  it('on the global file the own layer is global and the next one is the built-in default', () => {
    seedSourced(global(), { cooldown_group_sec: 30, signal_name: true }, { cooldown_group_sec: 'global', signal_name: 'default' });

    global().getState().inherit('cooldown_group_sec');
    global().getState().inherit('signal_name');
    global().getState().pin('signal_name');

    expect(global().getState().draft).toEqual({ cooldown_group_sec: null, signal_name: true });
  });
});

describe('apply', () => {
  beforeEach(() => {
    platformConsoleStore('qq').setState({ draft: {}, live: null, raw: '', notice: '', loadError: '', applyError: '', confirmation: null });
  });

  it('sends the merged document, clears the draft and waits for the bot to confirm', async () => {
    seedLive({ signal_name: false });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    const box = serveConfig({ signal_name: true });

    await platformConsoleStore('qq').getState().apply('token');

    expect(box.sent).toHaveLength(1);
    expect(box.sent[0]).toContain('version: 1');
    expect(box.sent[0]).toContain('    name: true');
    const state = platformConsoleStore('qq').getState();
    expect(state.draft).toEqual({});
    // bot 第一轮就认账，页面级提示里不该冒出交代认账的话。
    expect(state.notice).toBe('');
    expect(state.confirmation).toBe('confirmed');
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
    expect(state.applyError).toContain('无人消费的配置项');
    expect(state.saving).toBe(false);
  });
});

describe('save', () => {
  beforeEach(() => {
    platformConsoleStore('qq').setState({ draft: {}, live: null, raw: '', notice: '', loadError: '', applyError: '', confirmation: null });
    configStore(FEATURES_SCOPE).setState({ draft: {}, live: null, raw: '', notice: '', loadError: '', applyError: '', confirmation: null });
  });

  it('writes only the keys it was given and leaves the rest staged', async () => {
    seedLive({ signal_name: false, signal_at: true });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    platformConsoleStore('qq').getState().setDraft('signal_at', false);
    const box = serveConfig({ signal_name: true, signal_at: true });

    const outcome = await platformConsoleStore('qq').getState().save('token', ['signal_name']);

    expect(outcome).toEqual({ ok: true, message: '' });
    expect(box.sent[0]).toContain('    name: true');
    expect(box.sent[0]).not.toContain('at:');
    const state = platformConsoleStore('qq').getState();
    expect(state.draft).toEqual({ signal_at: false });
    // 结果留给区块自己显示，页面级的提示不动。
    expect(state.notice).toBe('');
    expect(state.applyError).toBe('');
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
    expect(platformConsoleStore('qq').getState().applyError).toBe('');
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

    // 认账的结论走 confirmation，message 只留后端的警告。
    expect(outcome).toEqual({ ok: true, message: '' });
    expect(configStore(FEATURES_SCOPE).getState().confirmation).toBe('changed');
    expect(requests.some((line) => line.startsWith('PUT ') && line.endsWith('/features/config/raw'))).toBe(true);
    expect(requests.filter((line) => line.includes('/platforms/'))).toEqual([]);
  });

  it('只把后端的警告交回给调用方', async () => {
    seedLive({ signal_name: false });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    const box = serveConfig({ signal_name: true });
    box.warnings = ['bot 没在跑，只校验了 YAML 语法'];

    const outcome = await platformConsoleStore('qq').getState().save('token', ['signal_name']);

    expect(outcome).toEqual({ ok: true, message: 'bot 没在跑，只校验了 YAML 语法' });
  });

  it('原文认不出、只能整份重写时接在警告后面交代一句', async () => {
    seedLive({ signal_name: false });
    platformConsoleStore('qq').getState().setDraft('signal_name', true);
    // 父键是流式映射，往它下面加子键就只能整份重写。
    const box = serveConfig({ signal_name: true }, { raw: 'trigger: {signals: {at: true}}\n' });
    box.warnings = ['bot 没在跑，只校验了 YAML 语法'];

    const outcome = await platformConsoleStore('qq').getState().save('token', ['signal_name']);

    expect(outcome).toEqual({
      ok: true,
      message: `bot 没在跑，只校验了 YAML 语法；${WHOLE_FILE_REWRITTEN}`,
    });
  });

  it('列表整块重写洗掉注释时也如实说一句', async () => {
    seedLive({ keyword_words: ['早安', '晚安'] });
    platformConsoleStore('qq').getState().setDraft('keyword_words', ['晚安', '午安', '夜安']);
    // 一次换掉好几项只能整块重写，夹在条目之间的注释就留不住了。
    serveConfig({ keyword_words: ['早安', '晚安'] }, {
      raw: ['trigger:', '  keyword:', '    words:', '      - 早安', '      # 这条是夜里用的', '      - 晚安', ''].join('\n'),
    });

    const outcome = await platformConsoleStore('qq').getState().save('token', ['keyword_words']);

    expect(outcome).toEqual({ ok: true, message: BLOCK_COMMENTS_DROPPED });
  });
});

// 写回之后到 bot 换上新文件之间：先锁着按钮快等一阵，没等到就放开按钮转后台，
// 等满一分钟还没换上才下「没确认」的结论。中间任何一步把状态搞错，界面都会骗人。
describe('认账', () => {
  const qq = () => platformConsoleStore('qq');

  beforeEach(() => { stage(qq(), { signal_name: false }); });
  afterEach(() => { vi.useRealTimers(); });

  it('写回成功就先记成等 bot 认账，值按写进去的那些显示', async () => {
    qq().getState().setDraft('signal_name', true);
    serveConfig({ signal_name: false }, { applied: false });

    vi.useFakeTimers();
    const applying = qq().getState().apply('token');
    await vi.advanceTimersByTimeAsync(250 * 3);

    const waiting = qq().getState();
    expect(waiting.confirmation).toBe('waiting');
    expect(waiting.saving).toBe(true);
    // 生效方公布的还是写回前的值，原样铺上去界面就闪回旧值了。
    expect(waiting.live?.state?.values).toEqual({ signal_name: true });
    expect(waiting.live?.applied).toBe(false);
    // 心跳这类实况仍按每次探到的换：探了三轮就是第三份快照。
    expect(waiting.live?.age_sec).toBe(3);

    await vi.advanceTimersByTimeAsync(CONFIRM_MS);
    await applying;
  });

  it('快等十二轮还没认账就放开按钮，结论留到后台等满再下', async () => {
    qq().getState().setDraft('signal_name', true);
    serveConfig({ signal_name: false }, { applied: false });

    vi.useFakeTimers();
    const applying = qq().getState().apply('token');
    await vi.advanceTimersByTimeAsync(QUICK_MS);
    await applying;

    expect(qq().getState().saving).toBe(false);
    expect(qq().getState().confirmation).toBe('waiting');

    await vi.advanceTimersByTimeAsync(CONFIRM_MS - QUICK_MS);

    expect(qq().getState().confirmation).toBe('unconfirmed');
    // 等满之后换上最后探到的快照：值回到 bot 实际在用的那些。
    expect(qq().getState().live?.state?.values).toEqual({ signal_name: false });
  });

  it('bot 中途换上新文件就收在认账上', async () => {
    qq().getState().setDraft('signal_name', true);
    const box = serveConfig({ signal_name: false }, { applied: false });

    vi.useFakeTimers();
    const applying = qq().getState().apply('token');
    await vi.advanceTimersByTimeAsync(QUICK_MS);
    await applying;
    expect(qq().getState().confirmation).toBe('waiting');

    box.applied = true;
    await vi.advanceTimersByTimeAsync(2000);

    expect(qq().getState().confirmation).toBe('confirmed');
    expect(qq().getState().live?.applied).toBe(true);
  });

  it('文件在写回之后又被别处改过就不再等，换上探到的那一份', async () => {
    qq().getState().setDraft('signal_name', true);
    const box = serveConfig({ signal_name: false }, { applied: false });
    box.override = 'version: 9\n';

    await qq().getState().apply('token');

    const state = qq().getState();
    expect(state.confirmation).toBe('changed');
    expect(state.raw).toBe('version: 9\n');
  });

  it('探不到生效方就是读不到，live 不动', async () => {
    qq().getState().setDraft('signal_name', true);
    const box = serveConfig({ signal_name: false }, { applied: false });
    box.stateDown = true;

    await qq().getState().apply('token');

    const state = qq().getState();
    expect(state.confirmation).toBe('unreadable');
    expect(state.applyError).toBe('');
    expect(state.live?.state?.values).toEqual({ signal_name: true });
  });

  it('新的一次写回顶掉旧的后台等待', async () => {
    qq().getState().setDraft('signal_name', true);
    const box = serveConfig({ signal_name: false }, { applied: false });

    vi.useFakeTimers();
    const first = qq().getState().apply('token');
    await vi.advanceTimersByTimeAsync(QUICK_MS);
    await first;
    expect(qq().getState().confirmation).toBe('waiting');

    box.applied = true;
    qq().getState().setDraft('signal_at', true);
    const second = qq().getState().apply('token');
    await vi.advanceTimersByTimeAsync(250);
    await second;
    expect(qq().getState().confirmation).toBe('confirmed');

    // 旧的那趟即使等满也无权再改结论。
    await vi.advanceTimersByTimeAsync(CONFIRM_MS);
    expect(qq().getState().confirmation).toBe('confirmed');
  });
});

// 认账出了岔子时要告诉人去服务器上看哪个进程。哪个进程按平台清单说。
describe('没确认生效的提示', () => {
  const OTHERCHAT: PlatformDescriptor = {
    id: 'otherchat',
    displayName: 'OtherChat',
    shortName: 'Other',
    channels: [{ name: 'other_group', kind: 'group', hashed: true }],
    hasConfig: true,
    entrypoints: ['otherchat_bot.py'],
    connectionPeer: 'OtherChat 网关',
    connectionHint: '检查网关地址',
  };

  /** 应用一次，让 bot 从头到尾不认账，返回落定之后要交代的那句话。 */
  const applyUnconfirmed = async (store: ConfigConsoleStore, scope: ConfigScope): Promise<string> => {
    stage(store, { signal_name: false });
    store.getState().setDraft('signal_name', true);
    serveConfig({ signal_name: false }, { applied: false });

    vi.useFakeTimers();
    try {
      const applying = store.getState().apply('token');
      await vi.advanceTimersByTimeAsync(QUICK_MS);
      await applying;
      // 快等这一段只放开按钮，还不下结论。
      expect(store.getState().confirmation).toBe('waiting');
      await vi.advanceTimersByTimeAsync(CONFIRM_MS - QUICK_MS);
    } finally {
      vi.useRealTimers();
    }
    expect(store.getState().confirmation).toBe('unconfirmed');
    return confirmationNote(scope, store.getState().confirmation);
  };

  afterEach(resetPlatforms);

  it('内置的 QQ 报它的入口脚本', async () => {
    expect(await applyUnconfirmed(platformConsoleStore('qq'), platformScope('qq'))).toBe(
      '已应用，但 bot 没在 60 秒内确认生效。它可能没在跑，去服务器上看这个平台的适配器进程（bot.py）。',
    );
  });

  it('后端新出的平台报它自己声明的入口脚本', async () => {
    adoptPlatforms([QQ_NAPCAT, OTHERCHAT]);

    expect(await applyUnconfirmed(platformConsoleStore('otherchat'), platformScope('otherchat'))).toBe(
      '已应用，但 bot 没在 60 秒内确认生效。它可能没在跑，去服务器上看这个平台的适配器进程（otherchat_bot.py）。',
    );
  });

  it('没声明入口的平台不留空括号', async () => {
    adoptPlatforms([QQ_NAPCAT, { ...OTHERCHAT, entrypoints: [] }]);

    expect(await applyUnconfirmed(platformConsoleStore('otherchat'), platformScope('otherchat'))).toBe(
      '已应用，但 bot 没在 60 秒内确认生效。它可能没在跑，去服务器上看这个平台的适配器进程。',
    );
  });

  it('清单里根本没有这个平台时也不留空括号', async () => {
    expect(await applyUnconfirmed(platformConsoleStore('ghost'), platformScope('ghost'))).toBe(
      '已应用，但 bot 没在 60 秒内确认生效。它可能没在跑，去服务器上看这个平台的适配器进程。',
    );
  });

  it('全局设置那套话不跟着平台走', async () => {
    expect(await applyUnconfirmed(configStore(FEATURES_SCOPE), FEATURES_SCOPE)).toBe(
      '已保存，但有在跑的 bot 没在 60 秒内换上新文件，「全局默认」页顶部列着是哪一个。',
    );
  });
});

describe('confirmationNote', () => {
  afterEach(resetPlatforms);

  it('认账了、还在等、没写过都不交代什么', () => {
    expect(confirmationNote(platformScope('qq'), 'confirmed')).toBe('');
    expect(confirmationNote(platformScope('qq'), 'waiting')).toBe('');
    expect(confirmationNote(platformScope('qq'), null)).toBe('');
  });

  it('平台页：文件被改过和读不到各有各的话', () => {
    expect(confirmationNote(platformScope('qq'), 'changed')).toBe('配置文件在应用之后又被改过，请核对当前内容。');
    expect(confirmationNote(platformScope('qq'), 'unreadable')).toBe('已应用，但暂时读不到 bot 的状态，还不知道是否生效。');
  });

  it('全局设置自己一套话', () => {
    expect(confirmationNote(FEATURES_SCOPE, 'changed')).toBe('全局文件在保存之后又被改过，请核对当前内容。');
    expect(confirmationNote(FEATURES_SCOPE, 'unreadable')).toBe('已保存，但暂时读不到全局设置的状态，还不知道各平台换没换上。');
  });
});

describe('refresh', () => {
  const qq = () => platformConsoleStore('qq');

  beforeEach(() => { stage(qq(), { signal_name: false }); });

  it('正在写回时不刷新', async () => {
    serveConfig({ signal_name: false });
    qq().setState({ saving: true });

    await qq().getState().refresh('token');

    expect(fetch).not.toHaveBeenCalled();
  });

  it('还在等 bot 认账时不刷新', async () => {
    // 这段时间读到的是换上新文件之前的值，铺上去会闪回旧值。
    serveConfig({ signal_name: false });
    qq().setState({ confirmation: 'waiting' });

    await qq().getState().refresh('token');

    expect(fetch).not.toHaveBeenCalled();
  });

  it('读到了只清读取失败，不碰应用的结果', async () => {
    serveConfig({ signal_name: false });
    qq().setState({ loadError: '上一次读不到', applyError: '上一次应用失败', notice: '已应用。' });

    await qq().getState().refresh('token');

    const state = qq().getState();
    expect(state.loadError).toBe('');
    expect(state.applyError).toBe('上一次应用失败');
    expect(state.notice).toBe('已应用。');
  });

  it('读失败只写读取失败这一项', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('连不上 supervisor'); }));
    qq().setState({ applyError: '上一次应用失败', notice: '已应用。' });

    await qq().getState().refresh('token');

    const state = qq().getState();
    expect(state.loadError).toBe('连不上 supervisor');
    expect(state.applyError).toBe('上一次应用失败');
    expect(state.notice).toBe('已应用。');
  });

  it('读到了就把「读不到」这个结论撤掉', async () => {
    serveConfig({ signal_name: false }, { applied: false });
    qq().setState({ confirmation: 'unreadable' });

    await qq().getState().refresh('token');

    expect(qq().getState().confirmation).toBeNull();
  });

  it('bot 后来换上了就把「没确认」撤掉', async () => {
    serveConfig({ signal_name: false }, { applied: true });
    qq().setState({ confirmation: 'unconfirmed' });

    await qq().getState().refresh('token');

    expect(qq().getState().confirmation).toBeNull();
  });

  it('bot 还是没换上就留着「没确认」', async () => {
    serveConfig({ signal_name: false }, { applied: false });
    qq().setState({ confirmation: 'unconfirmed' });

    await qq().getState().refresh('token');

    expect(qq().getState().confirmation).toBe('unconfirmed');
  });

  it('「文件被改过」不由定时刷新翻篇', async () => {
    // 读一遍不代表人核对过内容，这句得挂到人自己收起或者下一次写回。
    serveConfig({ signal_name: false }, { applied: true });
    qq().setState({ confirmation: 'changed' });

    await qq().getState().refresh('token');

    expect(qq().getState().confirmation).toBe('changed');
  });
});

// 结论说的是上一次写回到底生没生效，这件事不因为人又动了别处而变得不确定：
// 只有下一次写回、人自己收起、或者刷新读到相反的实况才作数。
describe('结论的去留', () => {
  const qq = () => platformConsoleStore('qq');

  beforeEach(() => { stage(qq(), { signal_name: false, cooldown_group_sec: 30 }); });

  it('改一项不碰结论，但清掉提示', () => {
    qq().setState({ confirmation: 'unconfirmed', notice: '已应用。' });

    qq().getState().setDraft('signal_name', true);

    expect(qq().getState().confirmation).toBe('unconfirmed');
    expect(qq().getState().notice).toBe('');
  });

  it('还在等的时候改动也照等', () => {
    qq().setState({ confirmation: 'waiting' });

    qq().getState().setDraft('signal_name', true);

    expect(qq().getState().confirmation).toBe('waiting');
  });

  it('钉一项不碰结论', () => {
    seedSourced(qq(), { cooldown_group_sec: 30 }, { cooldown_group_sec: 'global' });
    qq().setState({ confirmation: 'unconfirmed', notice: '已应用。' });

    qq().getState().pin('cooldown_group_sec');

    expect(qq().getState().confirmation).toBe('unconfirmed');
    expect(qq().getState().notice).toBe('');
  });

  it('全部丢弃清掉应用失败和提示，结论和读取失败都留着', () => {
    qq().setState({
      draft: { signal_name: true },
      confirmation: 'unconfirmed',
      applyError: '写不进去',
      notice: '已应用。',
      loadError: '读不到',
    });

    qq().getState().discard();

    const state = qq().getState();
    expect(state.draft).toEqual({});
    expect(state.applyError).toBe('');
    expect(state.notice).toBe('');
    expect(state.loadError).toBe('读不到');
    expect(state.confirmation).toBe('unconfirmed');
  });

  it('只丢几项也不碰结论', () => {
    qq().setState({ draft: { signal_name: true, cooldown_group_sec: 60 }, confirmation: 'changed' });

    qq().getState().discard(['signal_name']);

    expect(qq().getState().draft).toEqual({ cooldown_group_sec: 60 });
    expect(qq().getState().confirmation).toBe('changed');
  });

  it('人自己收起提示时连结论一起收', () => {
    qq().setState({ confirmation: 'changed', notice: '已应用。' });

    qq().getState().dismissNotice();

    expect(qq().getState().notice).toBe('');
    expect(qq().getState().confirmation).toBeNull();
  });

  it('还在等的结论收不掉', () => {
    // 后台还在探，收掉就等于把「已写入，等 bot 确认生效」这句提前撤了。
    qq().setState({ confirmation: 'waiting', notice: '已应用。' });

    qq().getState().dismissNotice();

    expect(qq().getState().confirmation).toBe('waiting');
  });

  it('下一次写回把上一次的结论换掉', async () => {
    qq().setState({ confirmation: 'unconfirmed' });
    qq().getState().setDraft('signal_name', true);
    serveConfig({ signal_name: true });

    await qq().getState().apply('token');

    expect(qq().getState().confirmation).toBe('confirmed');
  });
});

// 顶部那条状态条。它是「bot 有没有吃到这份配置」的唯一显示，报错了等于没有。
describe('生效状态条', () => {
  const live = (over: Record<string, unknown> = {}) =>
    ({ published: true, stale: false, applied: true, ...over }) as Parameters<typeof barState>[0];

  it('bot 认账了才是已生效', () => {
    expect(barState(live(), '', 'confirmed')).toEqual({ tone: 'live', text: '已生效' });
  });

  it('心跳断了不能显示成已生效', () => {
    expect(barState(live({ stale: true }), '', null).tone).toBe('halt');
  });

  it('bot 还在用旧配置也是红的', () => {
    expect(barState(live({ applied: false }), '', null).tone).toBe('halt');
  });

  // 写完到 bot 换上之间 applied 必然为假，按它判会亮红灯，看着像刚才那一下出了错。
  it('刚写回还在等认账时是灰的，不是红的', () => {
    expect(barState(live({ applied: false }), '', 'waiting')).toEqual({
      tone: 'idle', text: '已写入，等 bot 确认生效…',
    });
  });

  it('语法错误压过等认账', () => {
    expect(barState(live({ applied: false }), 'line 3: bad indent', 'waiting')).toEqual({
      tone: 'halt', text: '配置有语法错误，bot 仍在用上一份',
    });
  });

  it('心跳中断压过等认账', () => {
    expect(barState(live({ stale: true, applied: false }), '', 'waiting')).toEqual({
      tone: 'halt', text: 'bot 心跳已中断',
    });
  });

  // 语法错误压过一切：文件写坏时 applied 仍然为真（bot 用的是上一份），
  // 按 applied 判就会显示「已生效」，而实际上刚才那次改动一个字都没进去。
  it('语法错误压过 applied', () => {
    expect(barState(live(), 'line 3: bad indent', 'confirmed')).toEqual({
      tone: 'halt', text: '配置有语法错误，bot 仍在用上一份',
    });
  });

  it('bot 没上报时是灰的，不是红的', () => {
    expect(barState(null, '', null)).toEqual({ tone: 'idle', text: '等待 bot 上报' });
    expect(barState(null, '', 'waiting')).toEqual({ tone: 'idle', text: '等待 bot 上报' });
  });
});
