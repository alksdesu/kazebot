// [2026-06-01] Browser-local client preferences store.
// Why: auto-approval and rendering defaults are build-local frontend behavior and
// must not modify backend policy or session data. How: keep a small Zustand store
// backed by localStorage with explicit defaults and safe fallback rules. Purpose:
// each deployed frontend can choose its own approval and display preferences.
import { create } from 'zustand';

import { scopedKey } from './storageKey';

export type TitleGenerationMode = 'auto' | 'manual' | 'first-message';

export interface ClientPrefs {
  /** Keyed by real tool name, never by the backend policy operation. */
  autoApproveTools: Record<string, boolean>;
  titleGeneration: TitleGenerationMode;
  thinkingDefaultCollapsed: boolean;
  toolResultsDefaultCollapsed: boolean;
  /** Right panel width in px on desktop. */
  rightPanelWidth: number;
}

interface StoredClientPrefs extends ClientPrefs {
  version: number;
}

interface ClientPrefsState extends ClientPrefs {
  setAutoApproveTool: (toolName: string, enabled: boolean) => void;
  setTitleGeneration: (mode: TitleGenerationMode) => void;
  setThinkingDefaultCollapsed: (collapsed: boolean) => void;
  setToolResultsDefaultCollapsed: (collapsed: boolean) => void;
  setRightPanelWidth: (width: number) => void;
  resetClientPrefs: () => void;
}

export const RIGHT_PANEL_MIN_WIDTH = 240;
export const RIGHT_PANEL_MAX_WIDTH = 880;
export const RIGHT_PANEL_DEFAULT_WIDTH = 288;

export function clampRightPanelWidth(width: number): number {
  if (!Number.isFinite(width)) return RIGHT_PANEL_DEFAULT_WIDTH;
  return Math.min(RIGHT_PANEL_MAX_WIDTH, Math.max(RIGHT_PANEL_MIN_WIDTH, Math.round(width)));
}

const LS_KEY_CLIENT_PREFS = scopedKey('clonoth_client_prefs');

/** Bumped whenever autoApproveTools changes meaning and stored rules need migrating. */
const CLIENT_PREFS_VERSION = 2;

export const DEFAULT_AUTO_APPROVE_TOOLS: Record<string, boolean> = {
  read_file: true,
  search_in_files: true,
  list_dir: true,
  execute_command: false,
  write_file: false,
  apply_diff: false,
  request_restart: false,
};

// Backend tools do not send approvals under their own name: list_dir and
// search_in_files both request `read_file`, apply_diff requests `write_file`, and
// request_restart requests `restart`. A checkbox therefore only covers the operations
// listed here, so enabling search_in_files cannot auto-allow its replace-mode write.
// An unlisted operation always falls through to manual approval.
export const TOOL_APPROVAL_OPERATIONS: Record<string, readonly string[]> = {
  read_file: ['read_file'],
  list_dir: ['read_file'],
  search_in_files: ['read_file'],
  execute_command: ['execute_command'],
  write_file: ['write_file'],
  apply_diff: ['write_file'],
  request_restart: ['restart'],
};

export const DEFAULT_CLIENT_PREFS: ClientPrefs = {
  autoApproveTools: { ...DEFAULT_AUTO_APPROVE_TOOLS },
  titleGeneration: 'first-message',
  thinkingDefaultCollapsed: true,
  toolResultsDefaultCollapsed: true,
  rightPanelWidth: RIGHT_PANEL_DEFAULT_WIDTH,
};

function isTitleGenerationMode(value: unknown): value is TitleGenerationMode {
  return value === 'auto' || value === 'manual' || value === 'first-message';
}

// Frozen snapshot of the defaults that version 1 applied to policy operations. Migration
// must keep answering "was this already allowed back then", so it cannot read the live map.
const LEGACY_OPERATION_DEFAULTS: Readonly<Record<string, boolean>> = Object.freeze({
  read_file: true,
  search_in_files: true,
  list_dir: true,
  execute_command: false,
  write_file: false,
  apply_diff: false,
  request_restart: false,
});

export function migrateAutoApproveRules(stored: Record<string, boolean>): Record<string, boolean> {
  // Version 1 matched the policy operation, so a stored `write_file: true` also allowed
  // apply_diff, schedule writes, MCP client writes and secret writes. Keying by tool name
  // would silently promote rules that were inert before, so a rule survives only when every
  // operation it now covers was already allowed under the old operation-keyed matching.
  // Defaults are folded in first: a tool the user never touched still gains reach here, and
  // skipping it would let list_dir come alive for someone who had turned read_file off.
  const effective: Record<string, boolean> = { ...LEGACY_OPERATION_DEFAULTS, ...stored };
  const wasAllowed = (operation: string): boolean => effective[operation] === true;

  return Object.fromEntries(Object.entries(effective).map(([toolName, enabled]) => [
    toolName,
    enabled === true && (TOOL_APPROVAL_OPERATIONS[toolName] || [toolName]).every(wasAllowed),
  ]));
}

function readStoredPrefs(): { stored: Partial<ClientPrefs>; migrated: boolean } {
  // [2026-06-01] Why: localStorage may contain stale or hand-edited JSON.
  // How: parse defensively and keep only fields matching the current interface.
  // Purpose: a bad browser value cannot break application startup.
  try {
    const raw = localStorage.getItem(LS_KEY_CLIENT_PREFS);
    if (!raw) return { stored: {}, migrated: false };
    const parsed = JSON.parse(raw) as Partial<StoredClientPrefs>;
    const storedRules = parsed.autoApproveTools && typeof parsed.autoApproveTools === 'object'
      ? Object.fromEntries(Object.entries(parsed.autoApproveTools).map(([key, value]) => [key, value === true]))
      : undefined;
    const migrated = Boolean(storedRules) && parsed.version !== CLIENT_PREFS_VERSION;
    return {
      migrated,
      stored: {
        autoApproveTools: migrated && storedRules ? migrateAutoApproveRules(storedRules) : storedRules,
        titleGeneration: isTitleGenerationMode(parsed.titleGeneration) ? parsed.titleGeneration : undefined,
        thinkingDefaultCollapsed: typeof parsed.thinkingDefaultCollapsed === 'boolean' ? parsed.thinkingDefaultCollapsed : undefined,
        toolResultsDefaultCollapsed: typeof parsed.toolResultsDefaultCollapsed === 'boolean' ? parsed.toolResultsDefaultCollapsed : undefined,
        rightPanelWidth: typeof parsed.rightPanelWidth === 'number'
          ? clampRightPanelWidth(parsed.rightPanelWidth)
          : undefined,
      },
    };
  } catch {
    return { stored: {}, migrated: false };
  }
}

function mergePrefs(stored: Partial<ClientPrefs>): ClientPrefs {
  return {
    ...DEFAULT_CLIENT_PREFS,
    ...stored,
    autoApproveTools: {
      ...DEFAULT_AUTO_APPROVE_TOOLS,
      ...(stored.autoApproveTools || {}),
    },
  };
}

function hydratePrefs(): ClientPrefs {
  const { stored, migrated } = readStoredPrefs();
  const prefs = mergePrefs(stored);
  // Write the migrated map back right away so a later version never has to re-read v1 rules.
  if (migrated) persistPrefs(prefs);
  return prefs;
}

function persistPrefs(prefs: ClientPrefs) {
  // [2026-06-01] Why: Zustand persist middleware is unnecessary for this tiny store.
  // How: write the serialized public preference object after every setter. Purpose:
  // tests and runtime code can inspect one stable localStorage key.
  const stored: StoredClientPrefs = { ...prefs, version: CLIENT_PREFS_VERSION };
  localStorage.setItem(LS_KEY_CLIENT_PREFS, JSON.stringify(stored));
}

function publicPrefs(state: ClientPrefsState): ClientPrefs {
  return {
    autoApproveTools: state.autoApproveTools,
    titleGeneration: state.titleGeneration,
    thinkingDefaultCollapsed: state.thinkingDefaultCollapsed,
    toolResultsDefaultCollapsed: state.toolResultsDefaultCollapsed,
    rightPanelWidth: state.rightPanelWidth,
  };
}

/** One pending approval, identified the way the user sees it: the tool that raised it. */
export interface ApprovalMatch {
  toolName: string;
  operation?: string;
}

export function isAutoApproveToolEnabled(toolName: string, rules: Record<string, boolean>): boolean {
  // [2026-06-01] Why: unknown tools must remain manual even if defaults change.
  // How: first honor explicit localStorage rules, then fall back to the preset map,
  // and finally return false for every unrecognized tool. Purpose: local automation
  // stays conservative outside the known low-risk tool list.
  if (!toolName) return false;
  if (Object.prototype.hasOwnProperty.call(rules, toolName)) return rules[toolName] === true;
  if (Object.prototype.hasOwnProperty.call(DEFAULT_AUTO_APPROVE_TOOLS, toolName)) return DEFAULT_AUTO_APPROVE_TOOLS[toolName] === true;
  return false;
}

export function autoApproveCoversOperation(
  toolName: string,
  operation: string,
  rules: Record<string, boolean>,
): boolean {
  const requested = operation || toolName;
  if (requested === toolName) return true;
  const declared = TOOL_APPROVAL_OPERATIONS[toolName];
  if (declared) return declared.includes(requested);
  // A tool outside the curated table may borrow any operation, so it also needs the
  // operation itself enabled: ticking one unfamiliar tool cannot widen the blast radius.
  return isAutoApproveToolEnabled(requested, rules);
}

export function shouldAutoApproveToolCall(match: ApprovalMatch, rules: Record<string, boolean>): boolean {
  const { toolName, operation = '' } = match;
  return isAutoApproveToolEnabled(toolName, rules)
    && autoApproveCoversOperation(toolName, operation, rules);
}

export const useClientPrefsStore = create<ClientPrefsState>((set, get) => ({
  ...hydratePrefs(),

  setAutoApproveTool: (toolName, enabled) => set((state) => {
    const nextState = {
      ...state,
      autoApproveTools: { ...state.autoApproveTools, [toolName]: enabled },
    };
    persistPrefs(publicPrefs(nextState));
    return { autoApproveTools: nextState.autoApproveTools };
  }),

  setTitleGeneration: (mode) => set((state) => {
    const nextState = { ...state, titleGeneration: mode };
    persistPrefs(publicPrefs(nextState));
    return { titleGeneration: mode };
  }),

  setThinkingDefaultCollapsed: (collapsed) => set((state) => {
    const nextState = { ...state, thinkingDefaultCollapsed: collapsed };
    persistPrefs(publicPrefs(nextState));
    return { thinkingDefaultCollapsed: collapsed };
  }),

  setToolResultsDefaultCollapsed: (collapsed) => set((state) => {
    const nextState = { ...state, toolResultsDefaultCollapsed: collapsed };
    persistPrefs(publicPrefs(nextState));
    return { toolResultsDefaultCollapsed: collapsed };
  }),

  setRightPanelWidth: (width) => set((state) => {
    const next = clampRightPanelWidth(width);
    persistPrefs(publicPrefs({ ...state, rightPanelWidth: next }));
    return { rightPanelWidth: next };
  }),

  resetClientPrefs: () => {
    const next = { ...DEFAULT_CLIENT_PREFS, autoApproveTools: { ...DEFAULT_AUTO_APPROVE_TOOLS } };
    persistPrefs(next);
    set(next);
  },
}));

export { CLIENT_PREFS_VERSION, LS_KEY_CLIENT_PREFS };
export type { ClientPrefsState, StoredClientPrefs };
