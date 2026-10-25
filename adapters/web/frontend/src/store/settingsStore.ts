// [2026-05-16] Settings store — admin auth, node selection, model config, connection status.
// [2026-06-01] View mode state moved to viewStore.
// Why: chat/settings navigation is application shell state, while this store owns
// configuration data and the right-panel collapse flag. How: keep session editing
// in Header modal state and leave this store with only shared settings data.
// Purpose: App.tsx can use the view registry without a second routing system here.
import { create } from 'zustand';

import type { NodeDef } from '../types';
import { scopedKey } from './storageKey';

const LS_KEY_TOKEN = scopedKey('clonoth_admin_token');
/** 导出给 loadEntryNodes 复用：两处各写一遍字面量，加实例后缀时必漏一处。 */
export const LS_KEY_NODE = scopedKey('clonoth_entry_node');

type SessionProviderOverride = Record<string, unknown>;

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
  availableNodes: NodeDef[];
  modelConfig: { model: string; base_url: string; api_key_present: boolean } | null;
  // Active node tracking
  activeNodeId: string;
  activeNodeSessionId: string;
  activeNodeIsOverride: boolean;
  defaultNodeId: string;
  globalModel: string;
  globalBaseUrl: string;
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
  setAvailableNodes: (nodes: NodeDef[]) => void;
  setModelConfig: (cfg: { model: string; base_url: string; api_key_present: boolean } | null) => void;
  setActiveNode: (nodeId: string, isOverride: boolean, defaultId: string, sessionId?: string) => void;
  setGlobalConfig: (model: string, baseUrl: string) => void;
  setSessionProviderOverride: (override: SessionProviderOverride | null, sessionId?: string) => void;
  setRightPanelOpen: (open: boolean) => void;
}

export const useSettingsStore = create<SettingsState>((set) => ({
  adminToken: readStored(LS_KEY_TOKEN),
  isAuthenticated: false,
  isConnected: false,
  entryNodeId: readStored(LS_KEY_NODE) || '',
  storageWarning,
  availableNodes: [],
  modelConfig: null,
  activeNodeId: '',
  activeNodeSessionId: '',
  activeNodeIsOverride: false,
  defaultNodeId: '',
  globalModel: '',
  globalBaseUrl: '',
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
  setModelConfig: (cfg) => set({ modelConfig: cfg }),
  setActiveNode: (nodeId, isOverride, defaultId, sessionId = '') => set({ activeNodeId: nodeId, activeNodeSessionId: sessionId, activeNodeIsOverride: isOverride, defaultNodeId: defaultId }),
  setGlobalConfig: (model, baseUrl) => set({ globalModel: model, globalBaseUrl: baseUrl }),
  setSessionProviderOverride: (override, sessionId = '') => set({ sessionProviderOverride: override, sessionProviderOverrideSessionId: sessionId }),
  setRightPanelOpen: (open) => set({ rightPanelOpen: open }),
}));

export type { SessionProviderOverride, SettingsState };
