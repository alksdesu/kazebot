// 热载配置的草稿态。每份配置文件一份：平台配置、不分平台的全局默认，可以同时攒着没写回的改动。
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
  type CentralSwitchState,
  type ConfigSaveResult,
  type ConfigSource,
  type FeatureFallback,
  type FeaturesLiveState,
  type PlatformCapability,
  type PlatformRuntimeFacts,
} from '../api/supervisorClient';
import { entrypointNote, getPlatform } from '../platforms/registry';
import { FEATURES_SCOPE, platformScope, useConfigScope, type ConfigScope } from './configScope';
import { joinNotes } from './format';
import { rewriteYaml, type YamlRewrite } from './yamlRewrite';

/** 草稿按 live_config 的键名存（如 signal_at），不是 yaml 的点分路径。
 *
 * state 端点的 values 就是按键名给的，paths 负责键名 → 点分路径。写 yaml 时才转换，
 * 于是「界面读到的值」和「bot 公布的值」永远是同一个键空间。
 * 值为 null 表示把这一行从文件里删掉，交给下一层（平台交给全局，全局交给内置默认）。 */
export type Draft = Record<string, unknown>;

/** 一次写回的结果。ok=false 时 message 是错误；ok=true 时 message 是警告，生效方认没认账看 confirmation。 */
export interface CommitOutcome {
  ok: boolean;
  message: string;
}

/** 写回之后生效方认账到哪一步。waiting：已经写进文件，生效方还没换上；其余都是结论。
 *
 * 结论说的是最近那一次写回，跟着写回走：下一次写回才换掉，定时刷新读到的实况推翻了它才清掉。
 * 改动、丢弃草稿都不动它 —— 全局设置按块保存，别的块改一下就把这块的结论抹掉是不对的。 */
export type Confirmation = 'confirmed' | 'waiting' | 'unconfirmed' | 'changed' | 'unreadable';

export interface ConsoleState {
  live: ConfigLiveState | null;
  /** 磁盘上那份 yaml 的原文，写回时作为合并基底。 */
  raw: string;
  rawExists: boolean;
  loading: boolean;
  saving: boolean;
  /** 读取失败的原因，下一次读到就清掉。定时刷新只动它，不碰「应用」的结果。 */
  loadError: string;
  /** 「应用」失败的原因，挂到下一次应用或全部丢弃。 */
  applyError: string;
  /** 「应用」成功时附带的警告，改动或丢弃时清掉。 */
  notice: string;
  /** 最近一次写回的认账情况。null：还没写过，或者结论已被实况推翻、被手动收起。 */
  confirmation: Confirmation | null;
  draft: Draft;

  refresh: (token: string) => Promise<void>;
  setDraft: (name: string, value: unknown) => void;
  /** 删掉本层这一行，交给下一层。本层本来就没写这个键时，只是撤掉它的草稿。 */
  inherit: (name: string) => void;
  /** 把当前生效值写进本层，下一层以后怎么改都不再跟。本层本来就写着时，只是撤掉「删行」。 */
  pin: (name: string) => void;
  /** 不给 names 就丢掉全部草稿，连带收起「应用」的提示和失败原因；给了只丢这几项，区块自己的「丢弃」用，
   *  不碰页面级的提示 —— 区块保存的结果由区块自己拿着。 */
  discard: (names?: readonly string[]) => void;
  /** 平台的「应用」：全部草稿一起写回，结果挂在 notice / applyError 上给页面看。 */
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

/** 把草稿合进原文，返回要 PUT 上去的整份 yaml。只改草稿里那几个键，运营者手写的注释和空行照旧。
 *
 * paths 同时当白名单用：后端会拒绝无人消费的键，用它转换就不可能写出未知键。 */
export function mergeDraft(raw: string, draft: Draft, paths: Record<string, string>): YamlRewrite {
  const doc = parseDocument(raw);
  for (const [name, value] of Object.entries(draft)) {
    const path = paths[name];
    if (!path) throw new Error(`没有公布配置项 ${name}，拒绝写入`);
    setByPath(doc, path, value === null ? undefined : value);
  }
  return rewriteYaml(raw, doc, DUMP_OPTIONS);
}

const sleep = (ms: number) => new Promise((done) => { setTimeout(done, ms); });

const sameValue = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);

/** 把刚写进去的值叠到 live 上。键空间与 state 端点的 values 一致，可以直接铺。
 *
 * 写进去的键来源落到本层，来源切换和旁边的提示不必等生效方认账。旧版生效方不公布来源，那就不补。
 * 删行（null）不叠：删完落到哪一层的值只有生效方算得出来，先显示成 null 会闪一下默认值。 */
const withApplied = (live: ConfigLiveState | null, draft: Draft, layer: ConfigSource): ConfigLiveState | null => {
  if (!live?.state) return live;
  const written = Object.fromEntries(Object.entries(draft).filter(([, value]) => value !== null));
  const { sources } = live.state;
  return {
    ...live,
    applied: false,
    state: {
      ...live.state,
      values: { ...(live.state.values || {}), ...written },
      sources: sources && { ...sources, ...Object.fromEntries(Object.keys(written).map((name) => [name, layer])) },
    },
  };
};

interface ConfigApi {
  getState: (token: string) => Promise<ConfigLiveState>;
  getRaw: (token: string) => Promise<ConfigRawFile>;
  putRaw: (token: string, content: string, baseHash: string | null) => Promise<ConfigSaveResult>;
}

// 每次调用时才解引用接口函数：测试按模块替换它们，建 store 时拍下的引用会绕过替身。
const configApi = (scope: ConfigScope): ConfigApi => (
  scope.kind === 'features'
    ? {
        getState: (token) => getFeaturesState(token),
        getRaw: (token) => getFeaturesRaw(token),
        putRaw: (token, content, baseHash) => updateFeaturesRaw(token, content, baseHash),
      }
    : {
        getState: (token) => getPlatformState(token, scope.platformId),
        getRaw: (token) => getPlatformRaw(token, scope.platformId),
        putRaw: (token, content, baseHash) => updatePlatformRaw(token, scope.platformId, content, baseHash),
      }
);

// 写完先锁着按钮等一小会儿：生效方两秒对齐一次配置，常见情况这里就等到了。
const QUICK_ROUNDS = 12;
const QUICK_INTERVAL_MS = 250;
// 没等到就放开按钮、转到后台接着等。忙的 bot 会晚几轮对齐，从写完算起一分钟还没换上就不是忙的问题了。
const WATCH_INTERVAL_MS = 2000;
const CONFIRM_LIMIT_MS = 60_000;
const WATCH_ROUNDS = Math.ceil((CONFIRM_LIMIT_MS - QUICK_ROUNDS * QUICK_INTERVAL_MS) / WATCH_INTERVAL_MS);

type Verdict = Exclude<Confirmation, 'confirmed' | 'waiting'>;

const CONFIRM_LIMIT_TEXT = `${CONFIRM_LIMIT_MS / 1000} 秒`;

const FEATURES_WORDING: Record<Verdict, string> = {
  changed: '全局文件在保存之后又被改过，请核对当前内容。',
  unreadable: '已保存，但暂时读不到全局设置的状态，还不知道 bot 换没换上。',
  unconfirmed: `已保存，但有在跑的 bot 没在 ${CONFIRM_LIMIT_TEXT}内换上新文件，「全局默认」页顶部列着是哪一个。`,
};

/** 平台页没确认生效时，按平台声明的入口脚本告诉人去看哪个进程。 */
const confirmationWording = (scope: ConfigScope): Record<Verdict, string> => {
  if (scope.kind === 'features') return FEATURES_WORDING;
  const platform = getPlatform(scope.platformId);
  return {
    changed: '配置文件在应用之后又被改过，请核对当前内容。',
    unreadable: '已应用，但暂时读不到 bot 的状态，还不知道是否生效。',
    unconfirmed: `已应用，但 bot 没在 ${CONFIRM_LIMIT_TEXT}内确认生效。它可能没在跑，去服务器上看这个平台的适配器进程${platform ? entrypointNote(platform) : ''}。`,
  };
};

const isVerdict = (confirmation: Confirmation | null): confirmation is Verdict => (
  confirmation === 'changed' || confirmation === 'unreadable' || confirmation === 'unconfirmed'
);

/** 认账出了岔子时要交代的话。认账了、还在等、没写过都是空串 ——
 *  还在等由各页自己摆：平台页在状态条上，全局设置在区块的保存条上。 */
export function confirmationNote(scope: ConfigScope, confirmation: Confirmation | null): string {
  return isVerdict(confirmation) ? confirmationWording(scope)[confirmation] : '';
}

/** 定时刷新读到的实况推翻了上一次的结论：读不到的现在读到了，没认账的现在认了。 */
const verdictOutdated = (confirmation: Confirmation | null, state: ConfigLiveState): boolean => (
  confirmation === 'unreadable' || (confirmation === 'unconfirmed' && Boolean(state.applied))
);

/** 手动收起提示时连结论一起收；还在等的不是结论，照等。 */
const verdictDismissed = (confirmation: Confirmation | null): Partial<ConsoleState> => (
  confirmation === 'waiting' ? {} : { confirmation: null }
);

/** 刚写回的那一份：认账时要比对的原文、写进去的草稿、第几次写回。 */
interface Written {
  generation: number;
  content: string;
  subset: Draft;
}

/** 探一次的结果。applied 比的是生效方实际加载的那份配置的指纹，不是它看到的文件指纹 —— 后者在文件写坏时也等于当前文件。 */
type Probe =
  | { kind: 'confirmed' | 'changed' | 'waiting'; state: ConfigLiveState; file: ConfigRawFile }
  | { kind: 'unreadable' };

/** 每个作用域各自的代次。一份配置的刷新、写回不该作废另一份在飞的请求。 */
const refreshVersions = new Map<string, number>();
// 和刷新分开计：刷新作废的是在飞的读取，写回的代次作废的是上一次写回还在等的认账。
const writeVersions = new Map<string, number>();

const bump = (versions: Map<string, number>, scopeId: string): number => {
  const version = (versions.get(scopeId) || 0) + 1;
  versions.set(scopeId, version);
  return version;
};

const pick = (draft: Draft, names: readonly string[]): Draft => (
  Object.fromEntries(Object.entries(draft).filter(([name]) => names.includes(name)))
);

/** 每个作用域那份文件在分层里是哪一层。 */
const OWN_LAYER: Record<ConfigScope['kind'], ConfigSource> = { platform: 'platform', features: 'global' };

/** 生效方公布的来源。旧版生效方不公布，是 undefined。 */
const sourceOf = (live: ConfigLiveState | null, name: string): ConfigSource | undefined => (
  live?.state?.sources?.[name]
);

const createConsoleState = (scope: ConfigScope) => (
  set: (partial: Partial<ConsoleState>) => void,
  get: () => ConsoleState,
): ConsoleState => {
  const api = configApi(scope);
  const layer = OWN_LAYER[scope.kind];

  /** 这一次写回还是不是最新的一次。新的写回一开始，旧的那次就无权再改认账状态。 */
  const current = (written: Written): boolean => writeVersions.get(scope.id) === written.generation;

  /** 把草稿合进磁盘上最新那份原文写回。返回写进去的原文和要交代的警告。 */
  const write = async (token: string, live: ConfigLiveState | null, subset: Draft) => {
    const latest = await api.getRaw(token);
    const merged = mergeDraft(latest.content, subset, live?.state?.paths || {});
    // 合并的底子是刚读的这一版：读和写之间被共享组里别的实例改了，就报冲突，不拿旧底子盖掉。
    const result = await api.putRaw(token, merged.content, latest.hash);
    return {
      content: merged.content,
      warnings: merged.note ? [...result.warnings, merged.note] : result.warnings,
    };
  };

  /** 探一次：文件还是不是写进去的那份，生效方换上没有。 */
  const probe = async (token: string, content: string): Promise<Probe> => {
    try {
      const [state, file] = await Promise.all([api.getState(token), api.getRaw(token)]);
      if (file.content !== content) return { kind: 'changed', state, file };
      return { kind: state.applied ? 'confirmed' : 'waiting', state, file };
    } catch {
      return { kind: 'unreadable' };
    }
  };

  /**
   * 隔 interval 探一次，最多 rounds 次，探到结论就停。返回最后一次探测；被新的写回顶掉时返回 null。
   *
   * 还在等的时候换上最新的实况（心跳、bot 换没换上），值照旧叠着写进去的那些：
   * 生效方还没换上新文件，它公布的是写回前的值，原样铺上去界面就闪回旧值了。
   */
  const poll = async (token: string, written: Written, rounds: number, interval: number): Promise<Probe | null> => {
    let last: Probe | null = null;
    for (let round = 0; round < rounds; round += 1) {
      await sleep(interval);
      if (!current(written)) return null;
      last = await probe(token, written.content);
      if (!current(written)) return null;
      if (last.kind !== 'waiting') return last;
      set({ live: withApplied(last.state, written.subset, layer) });
    }
    return last;
  };

  /** 落下结论。认账、被别处改过、等满了没认账都换上最后探到的实况；读不到的没有实况可换。 */
  const conclude = (written: Written, result: Probe): void => {
    if (!current(written)) return;
    if (result.kind === 'unreadable') {
      set({ confirmation: 'unreadable' });
      return;
    }
    set({
      live: result.state,
      raw: result.file.content,
      rawExists: result.file.exists,
      loadError: '',
      confirmation: result.kind === 'waiting' ? 'unconfirmed' : result.kind,
    });
  };

  /** 后台接着等，等满了还没认账就算没确认。 */
  const watch = (token: string, written: Written): void => {
    poll(token, written, WATCH_ROUNDS, WATCH_INTERVAL_MS)
      .then((last) => { if (last) conclude(written, last); })
      // 这一趟没人 await，意外中断也得落个结论：一直挂在 waiting 上，定时刷新就再也不跑了。
      .catch(() => conclude(written, { kind: 'unreadable' }));
  };

  /** 认账：锁着按钮先等一小会儿，没等到就放开按钮转到后台。写已经成功了，这里出什么意外都不能报成写失败。 */
  const confirm = async (token: string, written: Written): Promise<void> => {
    try {
      const quick = await poll(token, written, QUICK_ROUNDS, QUICK_INTERVAL_MS);
      if (quick?.kind === 'waiting') watch(token, written);
      else if (quick) conclude(written, quick);
    } catch {
      conclude(written, { kind: 'unreadable' });
    }
  };

  /** 把一批草稿写回，再等生效方认账。 */
  const commit = async (token: string, subset: Draft): Promise<CommitOutcome | null> => {
    const { live, saving } = get();
    if (saving || !Object.keys(subset).length) return null;
    bump(refreshVersions, scope.id);
    const generation = bump(writeVersions, scope.id);
    set({ saving: true, loading: false, applyError: '', notice: '', confirmation: null });
    try {
      const saved = await write(token, live, subset);
      // 生效方隔一会儿才重读配置。这里先按刚写进去的值显示：
      // 草稿一清、live 又还是旧快照的话，界面会退回写回前的样子，看着像没存上。
      const pending = { ...get().draft };
      for (const [name, value] of Object.entries(subset)) {
        if (sameValue(pending[name], value)) delete pending[name];
      }
      set({
        draft: pending,
        raw: saved.content,
        rawExists: true,
        live: withApplied(live, subset, layer),
        confirmation: 'waiting',
      });
      await confirm(token, { generation, content: saved.content, subset });
      return { ok: true, message: joinNotes(...saved.warnings) };
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
    loadError: '',
    applyError: '',
    notice: '',
    confirmation: null,
    draft: {},

    refresh: async (token) => {
      const { saving, confirmation } = get();
      // 写完到认账之间读到的是换上新文件之前的值，铺上去会闪回旧值；这段时间认账那边自己在探。
      if (saving || confirmation === 'waiting') return;
      const version = bump(refreshVersions, scope.id);
      set({ loading: true });
      try {
        const [state, file] = await Promise.all([api.getState(token), api.getRaw(token)]);
        if (version !== refreshVersions.get(scope.id)) return;
        set({
          live: state,
          raw: file.content,
          rawExists: file.exists,
          loading: false,
          loadError: '',
          ...(verdictOutdated(get().confirmation, state) ? { confirmation: null } : {}),
        });
      } catch (error) {
        if (version !== refreshVersions.get(scope.id)) return;
        set({ loading: false, loadError: error instanceof Error ? error.message : '读取配置失败' });
      }
    },

    setDraft: (name, value) => {
      const { draft: current, live, saving } = get();
      const draft = { ...current };
      if (value === null) {
        // 删行只在本层确实写着这一行时才算改动；没写着就是本来就在跟随下一层。
        if (sourceOf(live, name) === layer) draft[name] = null;
        else delete draft[name];
      } else if (!saving && sameValue(value, live?.state?.values?.[name])) {
        // 改回生效值就不该继续算成待写回，否则「3 项待应用」里会混进已经撤销的改动。
        delete draft[name];
      } else {
        draft[name] = value;
      }
      set({ draft, notice: '' });
    },

    inherit: (name) => get().setDraft(name, null),

    // 和 setDraft 分开：钉的值和生效值一样也得写进本层，按值比较会把它当成没改。
    pin: (name) => {
      const { draft: current, live } = get();
      const draft = { ...current };
      if (sourceOf(live, name) === layer) {
        if (draft[name] === null) delete draft[name];
      } else if (!(name in draft) || draft[name] === null) {
        const applied = live?.state?.values?.[name];
        // 没公布这个值就钉不了：undefined 写进去等于删行。
        if (applied === undefined) return;
        draft[name] = applied;
      }
      set({ draft, notice: '' });
    },

    discard: (names) => {
      if (!names) {
        set({ draft: {}, notice: '', applyError: '' });
        return;
      }
      const draft = { ...get().draft };
      for (const name of names) delete draft[name];
      set({ draft });
    },

    apply: async (token) => {
      const outcome = await commit(token, get().draft);
      if (!outcome) return;
      set(outcome.ok ? { notice: outcome.message } : { applyError: outcome.message });
    },

    save: (token, names) => commit(token, pick(get().draft, names)),

    dismissNotice: () => set({ notice: '', ...verdictDismissed(get().confirmation) }),
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

/** 这个键现在该显示的值：有草稿看草稿，草稿是删行时看下一层，没草稿看生效值。哪儿都没有是 undefined。 */
export function displayedValue(
  state: ConsoleState,
  name: string,
  inherited?: Readonly<Record<string, unknown>>,
): unknown {
  if (!(name in state.draft)) return state.live?.state?.values?.[name];
  const value = state.draft[name];
  return value === null ? inherited?.[name] : value;
}

/** 删掉本层这一行之后落到的值。平台页落到全局那份；全局页落到内置默认，由各控件的 fallback 给。 */
function useInheritedValues(): Readonly<Record<string, unknown>> | undefined {
  const scope = useConfigScope();
  const values = configStore(FEATURES_SCOPE)((state) => state.live?.state?.values);
  return scope.kind === 'platform' ? values : undefined;
}

/** 编辑中的完整配置：生效值叠上草稿，删行的键换成下一层的值。试听要拿它当判定输入。
 *
 * 无条件全量叠加，不按当前页过滤 —— 漏掉一个键就等于试听和真实判定跑在两份配置上，
 * 而「试听说会回、实际不回」比没有试听更糟。下一层还没拉到时，删行的键先按现值算。 */
export function effectiveValues(
  state: ConsoleState,
  inherited?: Readonly<Record<string, unknown>>,
): Record<string, unknown> {
  const values: Record<string, unknown> = { ...(state.live?.state?.values || {}) };
  for (const name of Object.keys(state.draft)) {
    const value = displayedValue(state, name, inherited);
    if (value !== undefined) values[name] = value;
  }
  return values;
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

export interface LayerState {
  /** 生效方公布了这个键的来源。旧版不公布时界面不给切换，免得删错行。 */
  known: boolean;
  /** 写回之后这一项落在本层：草稿里有值，或者没有草稿而文件里本来就写着。 */
  own: boolean;
  source: ConfigSource | undefined;
}

const NO_FALLBACKS: readonly FeatureFallback[] = [];

export interface GlobalFallbacks {
  /** 全局文件没写这一项时，bot 眼下用着的兜底。只有全局页有，平台页上恒为空。 */
  fallbacks: readonly FeatureFallback[];
  /** 这一项是统一跑的开关时，它实际开不开。 */
  central: CentralSwitchState | undefined;
}

export function useGlobalFallbacks(name: string): GlobalFallbacks {
  // 各取公布快照里的原引用：选择器每次拼新数组会让订阅方空转。
  const fallbacks = useConsoleStore((state) => (state.live as FeaturesLiveState | null)?.fallbacks?.[name]);
  const central = useConsoleStore((state) => (state.live as FeaturesLiveState | null)?.central?.[name]);
  return { fallbacks: fallbacks || NO_FALLBACKS, central };
}

export function useLayerState(name: string): LayerState {
  const scope = useConfigScope();
  const source = useConsoleStore((state) => sourceOf(state.live, name));
  const drafted = useConsoleStore((state) => (name in state.draft ? state.draft[name] !== null : undefined));
  return { known: source !== undefined, own: drafted ?? source === OWN_LAYER[scope.kind], source };
}

/** 读一个键当前该显示的值：草稿优先，其次公布的生效值。
 *
 * fallback 只能是标量。传字面量数组或对象的话每次渲染都是新引用，
 * useSyncExternalStore 会当成 store 变了而空转 —— 列表类的值走 useWordList。 */
export function useLiveValue<T>(name: string, fallback: T): T {
  const inherited = useInheritedValues();
  return useConsoleStore((state) => {
    const value = displayedValue(state, name, inherited);
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
  const inherited = useInheritedValues();
  return useConsoleStore((state) => asWordList(displayedValue(state, name, inherited)));
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
  const inherited = useInheritedValues();
  return useConsoleStore((state) => {
    const value = displayedValue(state, name, inherited);
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

/** 各平台 live 状态都有的链路事实：连没连上、当前账号、积压条数。 */
export interface LinkFacts {
  connected: boolean;
  account: string;
  backlog: number;
}

export function readLinkFacts(state: ConsoleState): LinkFacts {
  const published = state.live?.state;
  const backlog = Number(published?.backlog ?? 0);
  return {
    connected: published?.connected === true,
    account: typeof published?.account === 'string' ? published.account : '',
    backlog: Number.isFinite(backlog) && backlog > 0 ? backlog : 0,
  };
}

export function useLinkFacts(): LinkFacts {
  const connected = useConsoleStore((state) => readLinkFacts(state).connected);
  const account = useConsoleStore((state) => readLinkFacts(state).account);
  const backlog = useConsoleStore((state) => readLinkFacts(state).backlog);
  return { connected, account, backlog };
}

/** 三态开关要能区分「没配」和「配成 false」，所以 null 不能被 fallback 吃掉。
 *
 * 抽成纯函数才测得到：hook 里那一层只是 store 订阅。 */
export function readTristate(
  state: ConsoleState,
  name: string,
  inherited?: Readonly<Record<string, unknown>>,
): boolean | null {
  const value = displayedValue(state, name, inherited);
  return value === undefined || value === null ? null : Boolean(value);
}

export function useTristate(name: string): boolean | null {
  const inherited = useInheritedValues();
  return useConsoleStore((state) => readTristate(state, name, inherited));
}
