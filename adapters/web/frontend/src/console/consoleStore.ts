// QQ 控制台的域切换与草稿态。「几项待应用」直接取草稿的大小。
import yaml from 'js-yaml';
import { create } from 'zustand';

import {
  getQqRaw,
  getQqState,
  updateQqRaw,
  type QqCapability,
  type QqInputRule,
  type QqLiveState,
  type QqRuntimeFacts,
} from '../api/supervisorClient';

export const CONSOLE_DOMAINS = [
  'account', 'channels', 'timing', 'permissions', 'persona', 'providers', 'models', 'runtime', 'memory',
] as const;
export type ConsoleDomain = (typeof CONSOLE_DOMAINS)[number];

export const DOMAIN_LABELS: Record<ConsoleDomain, string> = {
  account: '账号',
  channels: '信道',
  timing: '时机',
  permissions: '权限',
  persona: '人格',
  providers: '渠道',
  models: '模型',
  runtime: '运行',
  memory: '记忆',
};

/** 草稿按 live_config 的键名存（如 signal_at），不是 yaml 的点分路径。
 *
 * /qq/state 的 values 就是按键名给的，paths 负责键名 → 点分路径。写 yaml 时才转换，
 * 于是「界面读到的值」和「bot 公布的值」永远是同一个键空间。 */
export type Draft = Record<string, unknown>;

export interface ConsoleState {
  domain: ConsoleDomain;
  live: QqLiveState | null;
  /** 磁盘上那份 yaml 的原文，应用时作为合并基底。 */
  raw: string;
  rawExists: boolean;
  loading: boolean;
  saving: boolean;
  error: string;
  notice: string;
  draft: Draft;

  setDomain: (domain: ConsoleDomain) => void;
  refresh: (token: string) => Promise<void>;
  setDraft: (name: string, value: unknown) => void;
  discard: () => void;
  apply: (token: string) => Promise<void>;
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
  try {
    const loaded = yaml.load(raw);
    if (loaded && typeof loaded === 'object' && !Array.isArray(loaded)) {
      return loaded as Record<string, unknown>;
    }
  } catch {
    // 磁盘上的 yaml 写坏了也要能进控制台改回来，所以这里不抛。
  }
  return {};
}

/** 把点分路径写进嵌套对象。undefined 表示删掉这个键（回落默认值）。 */
export function setByPath(doc: Record<string, unknown>, path: string, value: unknown): void {
  const parts = path.split('.');
  const leaf = parts[parts.length - 1];
  let cursor: Record<string, unknown> = doc;
  for (const key of parts.slice(0, -1)) {
    const next = cursor[key];
    if (!next || typeof next !== 'object' || Array.isArray(next)) cursor[key] = {};
    cursor = cursor[key] as Record<string, unknown>;
  }
  if (value === undefined) delete cursor[leaf];
  else cursor[leaf] = value;
}

export function readByPath(doc: Record<string, unknown> | undefined, path: string): unknown {
  let cursor: unknown = doc;
  for (const key of path.split('.')) {
    if (!cursor || typeof cursor !== 'object' || Array.isArray(cursor)) return undefined;
    cursor = (cursor as Record<string, unknown>)[key];
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
    if (!path) throw new Error(`bot 没有公布配置项 ${name}，拒绝写入`);
    setByPath(doc, path, value);
  }
  return yaml.dump(doc, DUMP_OPTIONS);
}

const initialDomain = (): ConsoleDomain => {
  const requested = new URLSearchParams(window.location.search).get('domain');
  return (CONSOLE_DOMAINS as readonly string[]).includes(requested || '')
    ? (requested as ConsoleDomain)
    : 'timing';
};

// 域是从地址栏恢复的，切了不写回去会让刷新退回默认域。
const syncDomainQuery = (domain: ConsoleDomain): void => {
  const params = new URLSearchParams(window.location.search);
  params.set('domain', domain);
  window.history.replaceState(null, '', `${window.location.pathname}?${params.toString()}${window.location.hash}`);
};

const sleep = (ms: number) => new Promise((done) => { setTimeout(done, ms); });

/** 把刚写进去的值叠到 live 上。键空间与 /qq/state 的 values 一致，可以直接铺。 */
const withApplied = (live: QqLiveState | null, draft: Draft): QqLiveState | null => {
  if (!live?.state) return live;
  return {
    ...live,
    state: { ...live.state, values: { ...(live.state.values || {}), ...draft } },
  };
};

/**
 * 轮询到 bot 认账为止。
 *
 * applied 比的是 bot 实际加载的那份配置的指纹，不是它看到的文件指纹 —— 后者在文件
 * 写坏时也等于当前文件。中途没认账的快照一律不写回 state：那是保存前的值，
 * 覆盖上去界面就会闪回旧值，等于这个修复没做。
 */
const settle = async (
  token: string,
  set: (partial: Partial<ConsoleState>) => void,
  get: () => ConsoleState,
): Promise<void> => {
  for (let round = 0; round < 12; round += 1) {
    await sleep(250);
    try {
      const [state, file] = await Promise.all([getQqState(token), getQqRaw(token)]);
      if (state.applied) {
        set({ live: state, raw: file.content, rawExists: file.exists });
        return;
      }
    } catch {
      // 读不到就维持乐观值：文件已经写进去了，读状态失败不代表没保存。
      return;
    }
  }
  if (!get().error) {
    set({ notice: '已写入配置文件，但 bot 没在 3 秒内确认生效' });
  }
};

export const useConsoleStore = create<ConsoleState>((set, get) => ({
  domain: initialDomain(),
  live: null,
  raw: '',
  rawExists: false,
  loading: false,
  saving: false,
  error: '',
  notice: '',
  draft: {},

  setDomain: (domain) => {
    syncDomainQuery(domain);
    set({ domain });
  },

  refresh: async (token) => {
    set({ loading: true, error: '' });
    try {
      const [state, file] = await Promise.all([getQqState(token), getQqRaw(token)]);
      set({ live: state, raw: file.content, rawExists: file.exists, loading: false });
    } catch (error) {
      set({ loading: false, error: error instanceof Error ? error.message : '读取配置失败' });
    }
  },

  setDraft: (name, value) => {
    const draft = { ...get().draft };
    const applied = get().live?.state?.values?.[name];
    // 改回生效值就不该继续算成待应用，否则「3 项待应用」里会混进已经撤销的改动。
    if (JSON.stringify(value) === JSON.stringify(applied)) delete draft[name];
    else draft[name] = value;
    set({ draft, notice: '' });
  },

  discard: () => set({ draft: {}, notice: '', error: '' }),

  apply: async (token) => {
    const { raw, draft, live } = get();
    if (!Object.keys(draft).length) return;
    set({ saving: true, error: '', notice: '' });
    try {
      const paths = live?.state?.paths || {};
      const result = await updateQqRaw(token, mergeDraft(raw, draft, paths));
      // bot 每 2s 才重读一次配置并公布生效值。这里先按刚写进去的值显示：
      // 草稿一清、live 又还是旧快照的话，界面会退回保存前的样子，看着像没存上。
      set({
        draft: {},
        saving: false,
        notice: result.warnings.join('；'),
        live: withApplied(live, draft),
      });
      await settle(token, set, get);
    } catch (error) {
      set({ saving: false, error: error instanceof Error ? error.message : '保存失败' });
    }
  },

  dismissNotice: () => set({ notice: '' }),
}));

/** 编辑中的完整配置：bot 公布的生效值叠上草稿。试听要拿它当判定输入。
 *
 * 无条件全量叠加，不按当前页过滤 —— 漏掉一个键就等于试听和真实判定跑在两份配置上，
 * 而「试听说会回、实际不回」比没有试听更糟。 */
export function effectiveValues(state: ConsoleState): Record<string, unknown> {
  return { ...(state.live?.state?.values || {}), ...state.draft };
}

/** 读一个键当前该显示的值：草稿优先，其次 bot 公布的生效值。
 *
 * fallback 只能是标量。传字面量数组或对象的话每次渲染都是新引用，
 * useSyncExternalStore 会当成 store 变了而空转 —— 列表类的值走 useWordList。 */
export function useLiveValue<T>(name: string, fallback: T): T {
  return useConsoleStore((state) => {
    if (name in state.draft) return state.draft[name] as T;
    const value = state.live?.state?.values?.[name];
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

/** 读一个列表键的单项规则。bot 没公布就是 null —— 编不出来的规则不如不放行。
 *
 * 抽成纯函数才测得到：hook 里那一层只是 store 订阅。 */
export function readInputRule(state: ConsoleState, name: string): QqInputRule | null {
  return state.live?.state?.input_rules?.[name] || null;
}

export function useInputRule(name: string): QqInputRule | null {
  return useConsoleStore((state) => readInputRule(state, name));
}

/** 往词表里加一个词。规则来自 bot 公布的 input_rules，别在这儿重写一份。 */
export function addWord(
  words: readonly string[],
  raw: string,
  rule: QqInputRule | null,
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
export function idIsValid(raw: string, rule: QqInputRule | null): boolean {
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
  rule: QqInputRule | null,
): readonly number[] {
  if (!idIsValid(raw, rule)) return ids;
  const value = Number(raw.trim());
  return ids.includes(value) ? ids : [...ids, value];
}

/** bot 自己公布的键说明。前端重写一遍就会和后端漂移。 */
export function useNote(name: string): string {
  return useConsoleStore((state) => state.live?.state?.notes?.[name] || '');
}

const NO_CAPABILITIES: readonly QqCapability[] = [];

/** bot 公布的能力清单。拿不到就是空 —— 编不出来的清单不如不显示。 */
export function useCapabilities(): readonly QqCapability[] {
  return useConsoleStore((state) => state.live?.state?.capabilities || NO_CAPABILITIES);
}

const NO_RUNTIME: QqRuntimeFacts = {};

/** bot 公布的运行期事实。没上报时给同一个空对象，别每次造新的。 */
export function useRuntime(): QqRuntimeFacts {
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
