// [2026-06-01] Dedicated application view store for chat/settings mode.
// Why: settings navigation is an application shell concern, not model/admin
// configuration data. How: keep the active view and active settings tab in a small
// Zustand store. Purpose: App.tsx can select a registered view without growing new
// modal booleans or business conditionals.
import { create } from 'zustand';

export type ViewMode = 'chat' | 'settings' | 'console';

/** 走 AppLayout 三栏外壳的那几个域。console 自带外壳，不在其中。 */
export type ShellViewMode = Exclude<ViewMode, 'console'>;

export interface ViewState {
  viewMode: ViewMode;
  activeSettingsTab: string;
  openSettings: (tab?: string) => void;
  openConsole: () => void;
  closeSettings: () => void;
  setSettingsTab: (tab: string) => void;
}

const DEFAULT_SETTINGS_TAB = 'general';
const query = new URLSearchParams(window.location.search);
const REQUESTED_VIEW = query.get('view');
const initialViewMode: ViewMode =
  REQUESTED_VIEW === 'settings' || REQUESTED_VIEW === 'console' ? REQUESTED_VIEW : 'chat';
const initialSettingsTab = query.get('tab') || DEFAULT_SETTINGS_TAB;

function syncViewQuery(viewMode: ViewMode, tab: string): void {
  // Why: the view is restored from the query string on load, so leaving a stale
  // ?view= behind makes a refresh jump back to the view the user just left.
  const params = new URLSearchParams(window.location.search);
  if (viewMode === 'chat') {
    params.delete('view');
    params.delete('tab');
  } else {
    params.set('view', viewMode);
    if (viewMode === 'settings') params.set('tab', tab);
    else params.delete('tab');
  }
  // The console owns ?domain=; drop it whenever the console is not the active view.
  if (viewMode !== 'console') params.delete('domain');
  // replaceState, not pushState: view switches are navigation state, not history entries.
  const search = params.toString();
  window.history.replaceState(null, '', `${window.location.pathname}${search ? `?${search}` : ''}${window.location.hash}`);
}

export const useViewStore = create<ViewState>((set, get) => ({
  viewMode: initialViewMode,
  activeSettingsTab: initialSettingsTab,

  openSettings: (tab) => {
    // [2026-06-01] Opening settings optionally selects the requested tab first.
    // Why: Header node/model labels should land directly on the related settings
    // page. How: set both the view mode and tab in one store update. Purpose: the
    // shell swaps left and center content atomically.
    const nextTab = tab || DEFAULT_SETTINGS_TAB;
    syncViewQuery('settings', nextTab);
    set({ viewMode: 'settings', activeSettingsTab: nextTab });
  },

  openConsole: () => {
    syncViewQuery('console', get().activeSettingsTab);
    set({ viewMode: 'console' });
  },

  closeSettings: () => {
    // [2026-06-01] Closing settings returns to chat without erasing the tab.
    // Why: preserving the tab makes a later Settings click reopen where the user
    // left off if a caller does not request a tab. How: only change viewMode.
    // Purpose: the navigation state stays predictable and compact.
    syncViewQuery('chat', get().activeSettingsTab);
    set({ viewMode: 'chat' });
  },

  setSettingsTab: (tab) => {
    // [2026-06-01] Tab changes are local to the settings view.
    // Why: adding future settings pages should not require App.tsx changes. How:
    // store only the tab id and let SettingsPageHost resolve it through the tab
    // registry. Purpose: tab routing remains data-driven.
    if (get().viewMode === 'settings') syncViewQuery('settings', tab);
    set({ activeSettingsTab: tab });
  },
}));

export { DEFAULT_SETTINGS_TAB };
