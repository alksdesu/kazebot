// [2026-06-01] Dedicated application view store for chat/settings mode.
// Why: settings navigation is an application shell concern, not model/admin
// configuration data. How: keep the active view and active settings tab in a small
// Zustand store. Purpose: App.tsx can select a registered view without growing new
// modal booleans or business conditionals.
import { create } from 'zustand';

export type ViewMode = 'chat' | 'settings';

export interface ViewState {
  viewMode: ViewMode;
  activeSettingsTab: string;
  openSettings: (tab?: string) => void;
  closeSettings: () => void;
  setSettingsTab: (tab: string) => void;
}

const DEFAULT_SETTINGS_TAB = 'general';

// QQ 控制台曾经是 ?view=console&domain=x 的独立视图，那些链接还在书签和聊天记录里。
// 域名到 tab id 大多只差一个前缀，只有「运行」例外：设置页已经有一个 runtime 了。
const LEGACY_DOMAIN_TABS: Record<string, string> = { runtime: 'qq-link' };

export function tabForLegacyDomain(domain: string): string {
  return LEGACY_DOMAIN_TABS[domain] || `qq-${domain}`;
}

const query = new URLSearchParams(window.location.search);
const REQUESTED_VIEW = query.get('view');
const LEGACY_DOMAIN = REQUESTED_VIEW === 'console' ? query.get('domain') : null;
const initialViewMode: ViewMode =
  REQUESTED_VIEW === 'settings' || REQUESTED_VIEW === 'console' ? 'settings' : 'chat';
const initialSettingsTab = LEGACY_DOMAIN
  ? tabForLegacyDomain(LEGACY_DOMAIN)
  : query.get('tab') || DEFAULT_SETTINGS_TAB;

function syncViewQuery(viewMode: ViewMode, tab: string): void {
  // Why: the view is restored from the query string on load, so leaving a stale
  // ?view= behind makes a refresh jump back to the view the user just left.
  const params = new URLSearchParams(window.location.search);
  if (viewMode === 'chat') {
    params.delete('view');
    params.delete('tab');
  } else {
    params.set('view', viewMode);
    params.set('tab', tab);
  }
  // ?domain= 是控制台时代的定位参数，现在由 ?tab= 承担，留着会误导。
  params.delete('domain');
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

// 老链接进来先把地址栏改写成新参数，不然刷新一次又走一遍迁移，且 ?domain= 一直挂着。
if (REQUESTED_VIEW === 'console') syncViewQuery('settings', initialSettingsTab);

export { DEFAULT_SETTINGS_TAB };
