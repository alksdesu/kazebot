// 热载配置的草稿态。每份配置文件一份：各平台的配置、全平台共用的全局默认，可以同时攒着没写回的改动。
import yaml from 'js-yaml';
import { create, type StoreApi, type UseBoundStore } from 'zustand';

import {
  getFeaturesRaw,
  getFeaturesState,
  getPlatformRaw,
  getPlatformState,
  updateFeaturesRaw,
  updatePlatformRaw,
  type ConfigInputRule,
  type ConfigLiveState,
  type ConfigRawFile,
  type ConfigSaveResult,
  type PlatformCapability,
  type PlatformRuntimeFacts,
} from '../api/supervisorClient';
import { platformScope, useConfigScope, type ConfigScope } from './configScope';

/** 草稿按 live_config 的键名存（如 signal_at），不是 yaml 的点分路径。
 *
 * state 端点的 values 就是按键名给的，paths 负责键名 → 点分路径。写 yaml 时才转换，
 * 于是「界面读到的值」和「bot 公布的值」永远是同一个键空间。
 * 值为 null 表示把这一行从文件里删掉，交给下一层（平台交给全局，全局交给内置默认）。 */
export type Draft = Record<string, unknown>;

/** 一次写回的结果。ok=false 时 message 是错误；ok=true 时 message 是警告或「写进去了但没确认生效」。 */
export interface CommitOutcome {
  ok: boolean;
  message: string;
}

export interface ConsoleState {
  live: ConfigLiveState | null;
  /** 磁盘上那份 yaml 的原文，写回时作为合并基底。 */
  raw: string;
  rawExists: boolean;
  loading: boolean;
  saving: boolean;
  error: string;
  notice: string;
  draft: Draft;

  refresh: (token: string) => Promise<void>;
  setDraft: (name: string, value: unknown) => void;
  /** 不给 names 就丢掉全部草稿；给了只丢这几项，区块自己的「丢弃」用。 */
  discard: (names?: readonly string[]) => void;
  /** 平台的「应用」：全部草稿一起写回，结果挂在 notice / error 上给状态栏看。 */
  apply: (token: string) => Promise<void>;
  /** 区块的「保存」：只写回 names 里的草稿，结果交给调用方留在区块里。没有可写的返回 null。 */
  save: (token: string, names: readonly string[]) => Promise<CommitOutcome | null>;
  dismissNotice: () => void;
}

// 不设 noCompatMode：留着 js-yaml 的兼容引号，裸写的 on/off/yes/no 和 12:30 会被 YAML 1.1 读者当成布尔和 750。
const DUMP_OPTIONS: yaml.DumpOptions = {
  indent: 2,
  lineWidth: -1,
  noRefs: true,
  sortKeys: false,
  quotingType: '"',
  forceQuotes: false,
};

function parseDocument(raw: string): Record<string, unknown> {
  const loaded = yaml.load(raw);
  if (loaded === undefined || loaded === null) return {};
  if (typeof loaded !== 'object' || Array.isArray(loaded)) throw new Error('配置顶层必须是映射，请先修复 YAML，原文件未修改');
  return loaded as Record<string, unknown>;
}

const isMapping = (value: unknown): value is Record<string, unknown> => (
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)
);

/** 把点分路径写进嵌套对象。undefined 表示删掉这个键，删空的上级段一并去掉。 */
export function setByPath(doc: Record<string, unknown>, path: string, value: unknown): void {
  const parts = path.split('.');
  const leaf = parts[parts.length - 1];
  if (value === undefined) {
    const trail: Record<string, unknown>[] = [doc];
    for (const key of parts.slice(0, -1)) {
      const next = trail[trail.length - 1][key];
      if (!isMapping(next)) return;
      trail.push(next);
    }
    const parent = trail[trail.length - 1];
    // 文件里本来就没有这个键：原样不动，用户自己留的空段也不替他收。
    if (!Object.prototype.hasOwnProperty.call(parent, leaf)) return;
    delete parent[leaf];
    // 删完留下一串空段（trigger: {signals: {}}）看着像写坏了，从里往外收掉。
    for (let depth = trail.length - 1; depth > 0 && !Object.keys(trail[depth]).length; depth -= 1) {
      delete trail[depth - 1][parts[depth - 1]];
    }
    return;
  }
  let cursor: Record<string, unknown> = doc;
  for (const key of parts.slice(0, -1)) {
    const next = cursor[key];
    if (next !== undefined && !isMapping(next)) throw new Error(`配置路径 ${key} 不是映射，原文件未修改`);
    if (next === undefined) cursor[key] = {};
    cursor = cursor[key] as Record<string, unknown>;
  }
  cursor[leaf] = value;
}

export function readByPath(doc: Record<string, unknown> | undefined, path: string): unknown {
  let cursor: unknown = doc;
  for (const key of path.split('.')) {
    if (!isMapping(cursor)) return undefined;
    cursor = cursor[key];
  }
  return cursor;
}

/** 把草稿合进原文档，返回要 PUT 上去的整份 yaml。
 *
 * paths 同时当白名单用：后端会拒绝无人消费的键，用它转换就不可能写出未知键。 */
export function mergeDraft(raw: string, draft: Draft, paths: Record<string, string>): string {
  const doc = parseDocument(raw);
  for (const [name, value] of Object.entries(draft)) {
    const path = paths[name];
    if (!path) throw new Error(`没有公布配置项 ${name}，拒绝写入`);
    setByPath(doc, path, value === null ? undefined : value);
  }
  return yaml.dump(doc, DUMP_OPTIONS);
}

const sleep = (ms: number) => new Promise((done) => { setTimeout(done, ms); });

const sameValue = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);

/** 把刚写进去的值叠到 live 上。键空间与 state 端点的 values 一致，可以直接铺。
 *
 * 删行（null）不叠：删完落到哪一层的值只有生效方算得出来，先显示成 null 会闪一下默认值。 */
const withApplied = (live: ConfigLiveState | null, draft: Draft): ConfigLiveState | null => {
  if (!live?.state) return live;
  const written = Object.fromEntries(Object.entries(draft).filter(([, value]) => value !== null));
  return {
    ...live,
    applied: false,
    state: { ...live.state, values: { ...(live.state.values || {}), ...written } },
  };
};

interface ConfigApi {
  getState: (token: string) => Promise<ConfigLiveState>;
  getRaw: (token: string) => Promise<ConfigRawFile>;
  putRaw: (token: string, content: string) => Promise<ConfigSaveResult>;
}

// 每次调用时才解引用接口函数：测试按模块替换它们，建 store 时拍下的引用会绕过替身。
const configApi = (scope: ConfigScope): ConfigApi => (
  scope.kind === 'features'
    ? {
        getState: (token) => getFeaturesState(token),
        getRaw: (token) => getFeaturesRaw(token),
        putRaw: (token, content) => updateFeaturesRaw(token, content),
      }
    : {
        getState: (token) => getPlatformState(token, scope.platformId),
        getRaw: (token) => getPlatformRaw(token, scope.platformId),
        putRaw: (token, content) => updatePlatformRaw(token, scope.platformId, content),
      }
);

interface SettleWording {
  changed: string;
  unreadable: string;
  unconfirmed: string;
}

const WORDING: Record<ConfigScope['kind'], SettleWording> = {
  platform: {
    changed: '配置文件在应用之后又被改过，请刷新核对当前内容。',
    unreadable: '已应用，但暂时读不到 bot 的状态，无法确认是否生效，请稍后刷新。',
    unconfirmed: '已应用，但 bot 没在 3 秒内确认生效。它可能没在跑，去服务器上看这个平台的适配器进程（bot.py）。',
  },
  features: {
    changed: '全局文件在保存之后又被改过，请刷新核对当前内容。',
    unreadable: '已保存，但暂时读不到全局设置的状态，请稍后刷新。',
    unconfirmed: '已保存，但有在跑的 bot 没在 3 秒内换上新文件，页面顶部列着是哪一个。',
  },
};

/**
 * 轮询到生效方认账为止，返回要告诉用户的话（空串表示已确认生效）。
 *
 * applied 比的是实际加载的那份配置的指纹，不是看到的文件指纹 —— 后者在文件
 * 写坏时也等于当前文件。中途没认账的快照一律不写回 state：那是写回前的值，
 * 覆盖上去界面就会闪回旧值，等于这个修复没做。
 */
const settle = async (
  token: string,
  api: ConfigApi,
  wording: SettleWording,
  set: (partial: Partial<ConsoleState>) => void,
  savedRaw: string,
): Promise<string> => {
  for (let round = 0; round < 12; round += 1) {
    await sleep(250);
    try {
      const [state, file] = await Promise.all([api.getState(token), api.getRaw(token)]);
      if (file.content !== savedRaw) return wording.changed;
      if (state.applied) {
        set({ live: state, raw: file.content, rawExists: file.exists });
        return '';
      }
    } catch {
      return wording.unreadable;
    }
  }
  return wording.unconfirmed;
};

/** 每个作用域一份请求代次。一份配置的刷新不该作废另一份在飞的请求。 */
const refreshVersions = new Map<string, number>();

const bumpVersion = (scopeId: string): number => {
  const version = (refreshVersions.get(scopeId) || 0) + 1;
  refreshVersions.set(scopeId, version);
  return version;
};

const pick = (draft: Draft, names: readonly string[]): Draft => (
  Object.fromEntries(Object.entries(draft).filter(([name]) => names.includes(name)))
);

const createConsoleState = (scope: ConfigScope) => (
  set: (partial: Partial<ConsoleState>) => void,
  get: () => ConsoleState,
): ConsoleState => {
  const api = configApi(scope);

  /** 把一批草稿合进磁盘上最新那份原文写回，再等生效方认账。 */
  const commit = async (token: string, subset: Draft): Promise<CommitOutcome | null> => {
    const { live, saving } = get();
    if (saving || !Object.keys(subset).length) return null;
    bumpVersion(scope.id);
    set({ saving: true, loading: false, error: '', notice: '' });
    try {
      const paths = live?.state?.paths || {};
      const latest = await api.getRaw(token);
      const raw = mergeDraft(latest.content, subset, paths);
      const result = await api.putRaw(token, raw);
      // 生效方隔一会儿才重读配置。这里先按刚写进去的值显示：
      // 草稿一清、live 又还是旧快照的话，界面会退回写回前的样子，看着像没存上。
      const pending = { ...get().draft };
      for (const [name, value] of Object.entries(subset)) {
        if (sameValue(pending[name], value)) delete pending[name];
      }
      set({ draft: pending, raw, rawExists: true, live: withApplied(live, subset) });
      const settled = await settle(token, api, WORDING[scope.kind], set, raw);
      return { ok: true, message: [...result.warnings, settled].filter(Boolean).join('；') };
    } catch (error) {
      return { ok: false, message: error instanceof Error ? error.message : '写入失败' };
    } finally {
      set({ saving: false });
    }
  };

  return {
    live: null,
    raw: '',
    rawExists: false,
    loading: false,
    saving: false,
    error: '',
    notice: '',
    draft: {},

    refresh: async (token) => {
      if (get().saving) return;
      const version = bumpVersion(scope.id);
      set({ loading: true, error: '' });
      try {
        const [state, file] = await Promise.all([api.getState(token), api.getRaw(token)]);
        if (version !== refreshVersions.get(scope.id)) return;
        set({ live: state, raw: file.content, rawExists: file.exists, loading: false });
      } catch (error) {
        if (version !== refreshVersions.get(scope.id)) return;
        set({ loading: false, error: error instanceof Error ? error.message : '读取配置失败' });
      }
    },

    setDraft: (name, value) => {
      const draft = { ...get().draft };
      const applied = get().live?.state?.values?.[name];
      // 改回生效值就不该继续算成待写回，否则「3 项待应用」里会混进已经撤销的改动。
      if (!get().saving && sameValue(value, applied)) delete draft[name];
      else draft[name] = value;
      set({ draft, notice: '' });
    },

    discard: (names) => {
      if (!names) {
        set({ draft: {}, notice: '', error: '' });
        return;
      }
      const draft = { ...get().draft };
      for (const name of names) delete draft[name];
      set({ draft });
    },

    apply: async (token) => {
      const outcome = await commit(token, get().draft);
      if (!outcome) return;
      set(outcome.ok ? { notice: outcome.message } : { error: outcome.message });
    },

    save: (token, names) => commit(token, pick(get().draft, names)),

    dismissNotice: () => set({ notice: '' }),
  };
};

export type ConfigConsoleStore = UseBoundStore<StoreApi<ConsoleState>>;

const stores = new Map<string, ConfigConsoleStore>();

/** 取某个作用域的草稿态 store。按需建，建过就一直在 —— 换回去要看得到之前攒的改动。 */
export function configStore(scope: ConfigScope): ConfigConsoleStore {
  let store = stores.get(scope.id);
  if (!store) {
    store = create<ConsoleState>((set, get) => createConsoleState(scope)(set, get));
    stores.set(scope.id, store);
  }
  return store;
}

export function platformConsoleStore(platformId: string): ConfigConsoleStore {
  return configStore(platformScope(platformId));
}

/** 当前作用域的草稿态。分区里的控件都经它读写，不必知道自己写的是哪份文件。 */
export function useConsoleStore(): ConsoleState;
export function useConsoleStore<T>(selector: (state: ConsoleState) => T): T;
export function useConsoleStore<T>(selector?: (state: ConsoleState) => T): T | ConsoleState {
  const store = configStore(useConfigScope());
  return store(selector as (state: ConsoleState) => T);
}

/** 编辑中的完整配置：生效值叠上草稿。试听要拿它当判定输入。
 *
 * 无条件全量叠加，不按当前页过滤 —— 漏掉一个键就等于试听和真实判定跑在两份配置上，
 * 而「试听说会回、实际不回」比没有试听更糟。 */
export function effectiveValues(state: ConsoleState): Record<string, unknown> {
  return { ...(state.live?.state?.values || {}), ...state.draft };
}

/** 只读公布的生效值，不看草稿。分区摘要要的是「现在实际在跑什么」。 */
export function useAppliedValue<T>(name: string, fallback: T): T {
  return useConsoleStore((state) => {
    const value = state.live?.state?.values?.[name];
    return (value === undefined || value === null ? fallback : value) as T;
  });
}

/** 生效值里某个列表有几项。摘要只用得上个数，返回数字才不会让订阅方空转。 */
export function useAppliedCount(name: string): number {
  return useConsoleStore((state) => {
    const value = state.live?.state?.values?.[name];
    return Array.isArray(value) ? value.length : 0;
  });
}

/** 这个配置路径归不归某个分区管。按段比，`extensions.reactions` 不能吃掉 `extensions.reactions_x`。 */
export function pathBelongs(path: string, prefixes: readonly string[]): boolean {
  return prefixes.some((prefix) => path === prefix || path.startsWith(`${prefix}.`));
}

/** 某个分区名下有几项待写回。键名 → 路径由生效方公布，改了配置段归属这里自动跟着走。 */
export function countDraftIn(state: ConsoleState, prefixes: readonly string[]): number {
  const paths = state.live?.state?.paths || {};
  return Object.keys(state.draft)
    .filter((name) => pathBelongs(String(paths[name] || ''), prefixes)).length;
}

export function useSectionPending(prefixes: readonly string[]): number {
  return useConsoleStore((state) => countDraftIn(state, prefixes));
}

/** 这几个键里有几项草稿。区块自己保存时按它亮「保存」。 */
export function countDraftFor(state: ConsoleState, names: readonly string[]): number {
  return names.filter((name) => name in state.draft).length;
}

export function useDraftCount(names: readonly string[]): number {
  return useConsoleStore((state) => countDraftFor(state, names));
}

/** 读一个键当前该显示的值：草稿优先，其次公布的生效值。
 *
 * fallback 只能是标量。传字面量数组或对象的话每次渲染都是新引用，
 * useSyncExternalStore 会当成 store 变了而空转 —— 列表类的值走 useWordList。 */
export function useLiveValue<T>(name: string, fallback: T): T {
  return useConsoleStore((state) => {
    const value = name in state.draft ? state.draft[name] : state.live?.state?.values?.[name];
    return (value === undefined || value === null ? fallback : value) as T;
  });
}

// 引用稳定的空表。每次返回新的 [] 会让订阅方反复重渲染。
const EMPTY_LIST: readonly string[] = [];

// 过滤结果按源数组缓存：selector 每次返回新数组同样会让订阅方空转。
const wordListCache = new WeakMap<object, readonly string[]>();

/** 只留字符串项。非字符串渲染成一个看不见的空 chip，删别的词时又会被原样写回 yaml。
 *
 * 抽成纯函数才测得到：hook 里那一层只是 store 订阅。 */
export function asWordList(value: unknown): readonly string[] {
  if (!Array.isArray(value)) return EMPTY_LIST;
  if (value.every((item) => typeof item === 'string')) return value as string[];
  const cached = wordListCache.get(value);
  if (cached) return cached;
  const filtered = value.filter((item): item is string => typeof item === 'string');
  wordListCache.set(value, filtered);
  return filtered;
}

/** 读词表类的值（名字、关键词、前缀）。非数组一律落到同一个空表。 */
export function useWordList(name: string): readonly string[] {
  return useConsoleStore((state) =>
    asWordList(name in state.draft ? state.draft[name] : state.live?.state?.values?.[name]),
  );
}

/** 读一个列表键的单项规则。没公布就是 null —— 编不出来的规则不如不放行。
 *
 * 抽成纯函数才测得到：hook 里那一层只是 store 订阅。 */
export function readInputRule(state: ConsoleState, name: string): ConfigInputRule | null {
  return state.live?.state?.input_rules?.[name] || null;
}

export function useInputRule(name: string): ConfigInputRule | null {
  return useConsoleStore((state) => readInputRule(state, name));
}

/** 往词表里加一个词。规则来自公布的 input_rules，别在这儿重写一份。 */
export function addWord(
  words: readonly string[],
  raw: string,
  rule: ConfigInputRule | null,
): readonly string[] {
  if (rule?.kind !== 'word') return words;
  const token = rule.trim === false ? raw : raw.trim();
  if (!token || words.includes(token)) return words;
  return [...words, token];
}

// 同样引用稳定。号码表和词表分开两个空值，免得类型上互相串。
const EMPTY_IDS: readonly number[] = [];

/** 读号码表（群号、QQ 号）。 */
export function useIdList(name: string): readonly number[] {
  return useConsoleStore((state) => {
    const value = name in state.draft ? state.draft[name] : state.live?.state?.values?.[name];
    return Array.isArray(value) ? (value as number[]) : EMPTY_IDS;
  });
}

/** 判一个号码合不合规。IdList 要用它区分「不合规」和「已存在」两种拒绝文案。 */
export function idIsValid(raw: string, rule: ConfigInputRule | null): boolean {
  if (rule?.kind !== 'id' || !rule.pattern) return false;
  const token = raw.trim();
  if (!new RegExp(rule.pattern).test(token)) return false;
  const value = Number(token);
  // 不是重复 max：后端公布的上界再大，也不接受会在 JS 里失真的号码。
  if (!Number.isSafeInteger(value)) return false;
  if (rule.min !== undefined && value < rule.min) return false;
  if (rule.max !== undefined && value > rule.max) return false;
  return true;
}

/** 加一个号码。不合规就原样返回，调用方据此显示拒绝文案。 */
export function addId(
  ids: readonly number[],
  raw: string,
  rule: ConfigInputRule | null,
): readonly number[] {
  if (!idIsValid(raw, rule)) return ids;
  const value = Number(raw.trim());
  return ids.includes(value) ? ids : [...ids, value];
}

/** 生效方自己公布的键说明。前端重写一遍就会和后端漂移。 */
export function useNote(name: string): string {
  return useConsoleStore((state) => state.live?.state?.notes?.[name] || '');
}

const NO_CAPABILITIES: readonly PlatformCapability[] = [];

/** bot 公布的能力清单。拿不到就是空 —— 编不出来的清单不如不显示。 */
export function useCapabilities(): readonly PlatformCapability[] {
  return useConsoleStore((state) => state.live?.state?.capabilities || NO_CAPABILITIES);
}

const NO_RUNTIME: PlatformRuntimeFacts = {};

/** bot 公布的运行期事实。没上报时给同一个空对象，别每次造新的。 */
export function useRuntime(): PlatformRuntimeFacts {
  return useConsoleStore((state) => state.live?.state?.runtime || NO_RUNTIME);
}

/** 三态开关要能区分「没配」和「配成 false」，所以 null 不能被 fallback 吃掉。
 *
 * 抽成纯函数才测得到：hook 里那一层只是 store 订阅。 */
export function readTristate(state: ConsoleState, name: string): boolean | null {
  const value = name in state.draft ? state.draft[name] : state.live?.state?.values?.[name];
  return value === undefined || value === null ? null : Boolean(value);
}

export function useTristate(name: string): boolean | null {
  return useConsoleStore((state) => readTristate(state, name));
}
