// [2026-05-16] Settings store — admin auth, node selection, model config, connection status.
// [2026-06-01] View mode state moved to viewStore.
// Why: chat/settings navigation is application shell state, while this store owns
// configuration data and the right-panel collapse flag. How: keep session editing
// in Header modal state and leave this store with only shared settings data.
// Purpose: App.tsx can use the view registry without a second routing system here.
import { create } from 'zustand';

import type { AdminNode, ProvidersResponse } from '../api/supervisorClient';
import type { NodeDef } from '../types';
import { filterEntryNodes } from '../utils/entryNodes';
import { readStatus, type InheritanceSources } from '../utils/inheritedValue';
import { scopedKey } from './storageKey';

const LS_KEY_TOKEN = scopedKey('clonoth_admin_token');
/** 导出给 loadEntryNodes 复用：两处各写一遍字面量，加实例后缀时必漏一处。 */
export const LS_KEY_NODE = scopedKey('clonoth_entry_node');

type SessionProviderOverride = Record<string, unknown>;

/**
 * 顶栏、会话面板和登录后共用的一份数据读到哪了。loaded：读成过，手上的数据就是读到的那份；
 * error：还没读成过时最近一次没读成的原因。读成过以后哪次没读成都不再记：两处各读各的，
 * 后回来的失败不能把先回来的成功说成没读到。
 */
export interface SharedRead {
  readonly loaded: boolean;
  readonly error: string;
}

export const UNREAD: SharedRead = Object.freeze({ loaded: false, error: '' });
export const LOADED: SharedRead = Object.freeze({ loaded: true, error: '' });

const missed = (current: SharedRead, reason: string): SharedRead => (
  current.loaded || current.error === reason ? current : { loaded: false, error: reason }
);

const initialRightPanelOpen = () => window.innerWidth >= 1280;
let storageWarning = '';
function readStored(key: string): string | null {
  try { return localStorage.getItem(key); }
  catch { storageWarning = '浏览器存储不可用，登录和偏好仅在当前窗口保留。'; return null; }
}
function writeStored(key: string, value: string | null): string {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
    return '';
  } catch { return '浏览器存储不可用，登录和偏好仅在当前窗口保留。'; }
}

interface SettingsState {
  adminToken: string | null;
  storageWarning: string;
  isAuthenticated: boolean;
  isConnected: boolean;
  entryNodeId: string;
  /** 能当入口的节点，给节点下拉用。 */
  availableNodes: NodeDef[];
  /** 节点表里的全部节点：会话被切到不能当入口的节点上时，也要查得到它写的模型。 */
  allNodes: AdminNode[];
  /** allNodes 和 availableNodes 是同一次读节点表得来的。 */
  nodeListRead: SharedRead;
  modelConfig: { model: string; base_url: string; api_key_present: boolean } | null;
  // Active node tracking
  activeNodeId: string;
  activeNodeSessionId: string;
  activeNodeIsOverride: boolean;
  /**
   * 当前节点被写过几次。读当前节点前记下它，读回来时它变了，说明读的期间有人写过（面板刚切好的节点），
   * 读到的那份已经旧了。
   */
  activeNodeWrites: number;
  /** 后端配的默认入口：全局配置和当前节点状态都会带回来，是同一个值。 */
  defaultNodeId: string;
  globalModel: string;
  globalBaseUrl: string;
  globalConfigRead: SharedRead;
  /** 渠道表：节点或会话点名了渠道却没写模型时，用的是那个渠道块的模型和地址。 */
  providerTable: ProvidersResponse | null;
  providerTableRead: SharedRead;
  // [2026-06-01] Session-level provider override cache.
  // Why: the right panel shows the model/base_url that apply only to the current
  // session. How: store the latest provider_override object returned by Supervisor.
  // Purpose: Header, compact panel, and settings help read the same session-scoped
  // model state without duplicating fetch results.
  sessionProviderOverride: SessionProviderOverride | null;
  sessionProviderOverrideSessionId: string;
  // [2026-06-01] Right-panel visibility remains layout state shared by Header and
  // AppLayout. Why: viewStore selects which app view is active, but the right column
  // still needs an independent collapse flag. How: keep one boolean here. Purpose:
  // settings and chat views can both preserve the user's right-panel visibility.
  rightPanelOpen: boolean;

  setAdminToken: (token: string | null) => void;
  setAuthenticated: (v: boolean) => void;
  setConnected: (v: boolean) => void;
  setEntryNodeId: (id: string) => void;
  /** 只换入口清单。插件拿得到它，所以不改它的意思；读到节点表走 setNodeList。 */
  setAvailableNodes: (nodes: NodeDef[]) => void;
  /** 读到了节点表：两份清单一起换，标成读成过。 */
  setNodeList: (nodes: AdminNode[]) => void;
  failNodeList: (reason: string) => void;
  setModelConfig: (cfg: { model: string; base_url: string; api_key_present: boolean } | null) => void;
  setActiveNode: (nodeId: string, isOverride: boolean, defaultId: string, sessionId?: string) => void;
  /** 读到了全局配置：换数据，标成读成过。带了默认入口就一并换。 */
  setGlobalConfig: (model: string, baseUrl: string, defaultEntryId?: string) => void;
  failGlobalConfig: (reason: string) => void;
  setProviderTable: (table: ProvidersResponse) => void;
  failProviderTable: (reason: string) => void;
  setSessionProviderOverride: (override: SessionProviderOverride | null, sessionId?: string) => void;
  /** 退出登录：这次登录读到的节点、渠道、全局配置、会话状态连同读取状态一起回到没读过。 */
  clearLoginData: () => void;
  setRightPanelOpen: (open: boolean) => void;
}

export const useSettingsStore = create<SettingsState>((set) => ({
  adminToken: readStored(LS_KEY_TOKEN),
  isAuthenticated: false,
  isConnected: false,
  entryNodeId: readStored(LS_KEY_NODE) || '',
  storageWarning,
  availableNodes: [],
  allNodes: [],
  nodeListRead: UNREAD,
  modelConfig: null,
  activeNodeId: '',
  activeNodeSessionId: '',
  activeNodeIsOverride: false,
  activeNodeWrites: 0,
  defaultNodeId: '',
  globalModel: '',
  globalBaseUrl: '',
  globalConfigRead: UNREAD,
  providerTable: null,
  providerTableRead: UNREAD,
  sessionProviderOverride: null,
  sessionProviderOverrideSessionId: '',
  rightPanelOpen: initialRightPanelOpen(),

  setAdminToken: (token) => {
    const warning = writeStored(LS_KEY_TOKEN, token);
    set({ adminToken: token, storageWarning: warning });
  },
  setAuthenticated: (v) => set({ isAuthenticated: v }),
  setConnected: (v) => set({ isConnected: v }),
  setEntryNodeId: (id) => {
    const warning = writeStored(LS_KEY_NODE, id);
    set({ entryNodeId: id, storageWarning: warning });
  },
  setAvailableNodes: (nodes) => set({ availableNodes: nodes }),
  setNodeList: (nodes) => set({ allNodes: nodes, availableNodes: filterEntryNodes(nodes), nodeListRead: LOADED }),
  failNodeList: (reason) => set((state) => {
    const next = missed(state.nodeListRead, reason);
    return next === state.nodeListRead ? state : { nodeListRead: next };
  }),
  setModelConfig: (cfg) => set({ modelConfig: cfg }),
  setActiveNode: (nodeId, isOverride, defaultId, sessionId = '') => set((state) => ({
    activeNodeId: nodeId,
    activeNodeSessionId: sessionId,
    activeNodeIsOverride: isOverride,
    activeNodeWrites: state.activeNodeWrites + 1,
    defaultNodeId: defaultId,
  })),
  setGlobalConfig: (model, baseUrl, defaultEntryId) => set({
    globalModel: model,
    globalBaseUrl: baseUrl,
    globalConfigRead: LOADED,
    ...(defaultEntryId === undefined ? {} : { defaultNodeId: defaultEntryId }),
  }),
  failGlobalConfig: (reason) => set((state) => {
    const next = missed(state.globalConfigRead, reason);
    return next === state.globalConfigRead ? state : { globalConfigRead: next };
  }),
  setProviderTable: (table) => set({ providerTable: table, providerTableRead: LOADED }),
  failProviderTable: (reason) => set((state) => {
    const next = missed(state.providerTableRead, reason);
    return next === state.providerTableRead ? state : { providerTableRead: next };
  }),
  setSessionProviderOverride: (override, sessionId = '') => set({ sessionProviderOverride: override, sessionProviderOverrideSessionId: sessionId }),
  clearLoginData: () => set((state) => ({
    availableNodes: [],
    allNodes: [],
    nodeListRead: UNREAD,
    modelConfig: null,
    activeNodeId: '',
    activeNodeSessionId: '',
    activeNodeIsOverride: false,
    activeNodeWrites: state.activeNodeWrites + 1,
    defaultNodeId: '',
    globalModel: '',
    globalBaseUrl: '',
    globalConfigRead: UNREAD,
    providerTable: null,
    providerTableRead: UNREAD,
    sessionProviderOverride: null,
    sessionProviderOverrideSessionId: '',
  })),
  setRightPanelOpen: (open) => set({ rightPanelOpen: open }),
}));

/** 某个会话的消息交给哪个节点。 */
export interface SessionNode {
  /** 有会话时，这个会话的节点读到了没有。 */
  readonly known: boolean;
  readonly nodeId: string;
  readonly isOverride: boolean;
}

/**
 * 有会话时只认读到的那份，还没读到 known 就是 false，不拿入口节点顶上；新对话依次是这个浏览器
 * 刚选的、入口节点、后端默认入口：网页发消息时两个都空就不带 entry_node_id，后端用的正是默认入口。
 */
export function sessionNodeOf(state: SettingsState, sid: string): SessionNode {
  if (sid) {
    if (state.activeNodeSessionId !== sid) return { known: false, nodeId: '', isOverride: false };
    return { known: true, nodeId: state.activeNodeId || '', isOverride: Boolean(state.activeNodeIsOverride) };
  }
  const local = !state.activeNodeSessionId;
  return {
    known: true,
    nodeId: (local ? state.activeNodeId : '') || state.entryNodeId || state.defaultNodeId || '',
    isOverride: local && Boolean(state.activeNodeIsOverride),
  };
}

/** 某个会话的模型覆盖。新对话没有会话，也就没有覆盖；有会话时 store 里存的不是它的就是还没读到。 */
export function sessionOverrideOf(state: SettingsState, sid: string): { known: boolean; override: SessionProviderOverride | null } {
  if (!sid) return { known: true, override: null };
  if (state.sessionProviderOverrideSessionId !== sid) return { known: false, override: null };
  return { known: true, override: state.sessionProviderOverride };
}

/**
 * 节点表里这个节点的那一行：先认文件名（id），再认 aliases 里的旧 id，和 engine 加载节点的认法一样。
 * declared_id 是 yaml 里写的 id，engine 不按它找节点，这里也不认。
 */
export function nodeRowOf(state: SettingsState, nodeId: string): AdminNode | null {
  if (!nodeId) return null;
  return state.allNodes.find((node) => node.id === nodeId)
    ?? state.allNodes.find((node) => Array.isArray(node.aliases) && node.aliases.includes(nodeId))
    ?? null;
}

/** 会话的节点，连同它在节点表里的那一行。 */
export interface SessionNodeRow extends SessionNode {
  readonly row: AdminNode | null;
  /** 节点表读成了，表里却认不出这个节点（删了、改了名）：engine 也加载不到它。 */
  readonly missing: boolean;
}

export function sessionNodeRowOf(state: SettingsState, sid: string): SessionNodeRow {
  const node = sessionNodeOf(state, sid);
  const row = node.known ? nodeRowOf(state, node.nodeId) : null;
  return { ...node, row, missing: node.known && Boolean(node.nodeId) && state.nodeListRead.loaded && !row };
}

/** 节点显示成什么：表里那一行的名字，表里认不出就照实说。还没读到或没有节点时给空串，由调用方说。 */
export function sessionNodeLabel(node: SessionNodeRow): string {
  if (!node.known || !node.nodeId) return '';
  if (node.row) return node.row.name || node.row.id;
  return node.missing ? `${node.nodeId}（找不到这个节点）` : node.nodeId;
}

/** 继承链的会话覆盖那一层，取 store 里读到的那份。overrideError 是调用方读这个会话的覆盖时没读成的原因。 */
export function sessionLayerOf(state: SettingsState, sid: string, overrideError: string): InheritanceSources['session'] {
  const { known, override } = sessionOverrideOf(state, sid);
  return { status: known ? 'loaded' : overrideError ? 'failed' : 'loading', fields: override };
}

/** 这个会话的继承链要的四份数据。会话覆盖那一层由调用方给，nodeError 是调用方读这个会话的节点时没读成的原因。 */
export function inheritanceSourcesOf(
  state: SettingsState,
  sid: string,
  session: InheritanceSources['session'],
  nodeError: string,
): InheritanceSources {
  const node = sessionNodeRowOf(state, sid);
  return {
    session,
    node: {
      status: !node.known ? (nodeError ? 'failed' : 'loading') : !node.nodeId ? 'loaded' : node.missing ? 'missing' : readStatus(state.nodeListRead),
      id: node.row?.id ?? node.nodeId,
      fields: node.row,
    },
    channels: {
      status: readStatus(state.providerTableRead),
      table: state.providerTable?.providers ?? null,
      active: state.providerTable?.active_provider ?? '',
    },
    global: { status: readStatus(state.globalConfigRead), model: state.globalModel, baseUrl: state.globalBaseUrl },
  };
}

export type { SessionProviderOverride, SettingsState };
