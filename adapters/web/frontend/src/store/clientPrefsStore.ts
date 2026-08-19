// [2026-06-01] Browser-local client preferences store.
// Why: auto-approval and rendering defaults are build-local frontend behavior and
// must not modify backend policy or session data. How: keep a small Zustand store
// backed by localStorage with explicit defaults and safe fallback rules. Purpose:
// each deployed frontend can choose its own approval and display preferences.
import { create } from 'zustand';

export type TitleGenerationMode = 'auto' | 'manual' | 'first-message';

export interface ClientPrefs {
  autoApproveTools: Record<string, boolean>;
  titleGeneration: TitleGenerationMode;
  thinkingDefaultCollapsed: boolean;
  toolResultsDefaultCollapsed: boolean;
}

interface ClientPrefsState extends ClientPrefs {
  setAutoApproveTool: (toolName: string, enabled: boolean) => void;
  setTitleGeneration: (mode: TitleGenerationMode) => void;
  setThinkingDefaultCollapsed: (collapsed: boolean) => void;
  setToolResultsDefaultCollapsed: (collapsed: boolean) => void;
  resetClientPrefs: () => void;
}

const LS_KEY_CLIENT_PREFS = 'clonoth_client_prefs';

export const DEFAULT_AUTO_APPROVE_TOOLS: Record<string, boolean> = {
  read_file: true,
  search_in_files: true,
  list_dir: true,
  execute_command: false,
  write_file: false,
  apply_diff: false,
  request_restart: false,
};

export const DEFAULT_CLIENT_PREFS: ClientPrefs = {
  autoApproveTools: { ...DEFAULT_AUTO_APPROVE_TOOLS },
  titleGeneration: 'first-message',
  thinkingDefaultCollapsed: true,
  toolResultsDefaultCollapsed: true,
};

function isTitleGenerationMode(value: unknown): value is TitleGenerationMode {
  return value === 'auto' || value === 'manual' || value === 'first-message';
}

function readStoredPrefs(): Partial<ClientPrefs> {
  // [2026-06-01] Why: localStorage may contain stale or hand-edited JSON.
  // How: parse defensively and keep only fields matching the current interface.
  // Purpose: a bad browser value cannot break application startup.
  try {
    const raw = localStorage.getItem(LS_KEY_CLIENT_PREFS);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Partial<ClientPrefs>;
    return {
      autoApproveTools: parsed.autoApproveTools && typeof parsed.autoApproveTools === 'object'
        ? Object.fromEntries(Object.entries(parsed.autoApproveTools).map(([key, value]) => [key, value === true]))
        : undefined,
      titleGeneration: isTitleGenerationMode(parsed.titleGeneration) ? parsed.titleGeneration : undefined,
      thinkingDefaultCollapsed: typeof parsed.thinkingDefaultCollapsed === 'boolean' ? parsed.thinkingDefaultCollapsed : undefined,
      toolResultsDefaultCollapsed: typeof parsed.toolResultsDefaultCollapsed === 'boolean' ? parsed.toolResultsDefaultCollapsed : undefined,
    };
  } catch {
    return {};
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

function persistPrefs(prefs: ClientPrefs) {
  // [2026-06-01] Why: Zustand persist middleware is unnecessary for this tiny store.
  // How: write the serialized public preference object after every setter. Purpose:
  // tests and runtime code can inspect one stable localStorage key.
  localStorage.setItem(LS_KEY_CLIENT_PREFS, JSON.stringify(prefs));
}

function publicPrefs(state: ClientPrefsState): ClientPrefs {
  return {
    autoApproveTools: state.autoApproveTools,
    titleGeneration: state.titleGeneration,
    thinkingDefaultCollapsed: state.thinkingDefaultCollapsed,
    toolResultsDefaultCollapsed: state.toolResultsDefaultCollapsed,
  };
}

export function shouldAutoApproveTool(toolName: string, rules: Record<string, boolean>): boolean {
  // [2026-06-01] Why: unknown tools must remain manual even if defaults change.
  // How: first honor explicit localStorage rules, then fall back to the preset map,
  // and finally return false for every unrecognized tool. Purpose: local automation
  // stays conservative outside the known low-risk tool list.
  if (Object.prototype.hasOwnProperty.call(rules, toolName)) return rules[toolName] === true;
  if (Object.prototype.hasOwnProperty.call(DEFAULT_AUTO_APPROVE_TOOLS, toolName)) return DEFAULT_AUTO_APPROVE_TOOLS[toolName] === true;
  return false;
}

export const useClientPrefsStore = create<ClientPrefsState>((set, get) => ({
  ...mergePrefs(readStoredPrefs()),

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

  resetClientPrefs: () => {
    const next = { ...DEFAULT_CLIENT_PREFS, autoApproveTools: { ...DEFAULT_AUTO_APPROVE_TOOLS } };
    persistPrefs(next);
    set(next);
  },
}));

export { LS_KEY_CLIENT_PREFS };
export type { ClientPrefsState };
