// [2026-06-01] Dedicated application view store for chat/settings mode.
// Why: settings navigation is an application shell concern, not model/admin
// configuration data. How: keep the active view and active settings tab in a small
// Zustand store. Purpose: App.tsx can select a registered view without growing new
// modal booleans or business conditionals.
import { create } from 'zustand';
import { resolveSettingsLocation } from '../components/settings/settingsTabAliases';
import { confirmNavigation } from '../hooks/useUnsavedChanges';
import { commandAction } from '../plugins/commands';

export type ViewMode = string;

export interface ViewState {
  viewMode: ViewMode;
  activeSettingsTab: string;
  /** 平台页要展开并滚到哪个分区。留在地址栏里，刷新之后还停在同一块。 */
  activeSettingsSection: string;
  openSettings: (tab?: string, section?: string) => void;
  closeSettings: () => void;
  setSettingsTab: (tab: string, section?: string) => void;
  openWorkspace: (view: string) => void;
}

const DEFAULT_SETTINGS_TAB = 'general';

// QQ 控制台曾经是 ?view=console&domain=x 的独立视图，那些链接还在书签和聊天记录里。
// 域名到 tab id 大多只差一个前缀，只有「运行」例外：设置页已经有一个 runtime 了。
const LEGACY_DOMAIN_TABS: Record<string, string> = { runtime: 'qq-link' };

export function locationForLegacyDomain(domain: string) {
  return resolveSettingsLocation(LEGACY_DOMAIN_TABS[domain] || `qq-${domain}`);
}

export function tabForLegacyDomain(domain: string): string {
  return locationForLegacyDomain(domain).tab;
}

const query = new URLSearchParams(window.location.search);
const REQUESTED_VIEW = query.get('view');
const LEGACY_DOMAIN = REQUESTED_VIEW === 'console' ? query.get('domain') : null;
const initialViewMode: ViewMode =
  REQUESTED_VIEW === 'settings' || REQUESTED_VIEW === 'console' ? 'settings' :
    REQUESTED_VIEW || 'chat';
const initialLocation = LEGACY_DOMAIN
  ? locationForLegacyDomain(LEGACY_DOMAIN)
  : resolveSettingsLocation(query.get('tab') || DEFAULT_SETTINGS_TAB, query.get('section') || '');

function syncViewQuery(viewMode: ViewMode, tab: string, section: string): void {
  // Why: the view is restored from the query string on load, so leaving a stale
  // ?view= behind makes a refresh jump back to the view the user just left.
  const params = new URLSearchParams(window.location.search);
  if (viewMode === 'chat') {
    params.delete('view');
    params.delete('tab');
    params.delete('section');
  } else {
    params.set('view', viewMode);
    if (viewMode === 'settings') params.set('tab', tab);
    else params.delete('tab');
    if (viewMode === 'settings' && section) params.set('section', section);
    else params.delete('section');
  }
  // ?domain= 是控制台时代的定位参数，现在由 ?tab= 承担，留着会误导。
  params.delete('domain');
  // replaceState, not pushState: view switches are navigation state, not history entries.
  const search = params.toString();
  window.history.replaceState(null, '', `${window.location.pathname}${search ? `?${search}` : ''}${window.location.hash}`);
}

export const useViewStore = create<ViewState>((set, get) => ({
  viewMode: initialViewMode,
  activeSettingsTab: initialLocation.tab,
  activeSettingsSection: initialLocation.section,
  openWorkspace: commandAction('navigation.workspace', (viewMode: string) => {
    if (viewMode !== get().viewMode && !confirmNavigation()) return;
    syncViewQuery(viewMode, get().activeSettingsTab, get().activeSettingsSection);
    set({ viewMode });
  }),

  openSettings: commandAction('navigation.settings', (tab?: string, section?: string) => {
    // [2026-06-01] Opening settings optionally selects the requested tab first.
    // Why: Header node/model labels should land directly on the related settings
    // page. How: set both the view mode and tab in one store update. Purpose: the
    // shell swaps left and center content atomically.
    const next = resolveSettingsLocation(tab || get().activeSettingsTab || DEFAULT_SETTINGS_TAB, section);
    if ((get().viewMode !== 'settings' || next.tab !== get().activeSettingsTab) && !confirmNavigation()) return;
    syncViewQuery('settings', next.tab, next.section);
    set({ viewMode: 'settings', activeSettingsTab: next.tab, activeSettingsSection: next.section });
  }),

  closeSettings: commandAction('navigation.closeSettings', () => {
    if (get().viewMode !== 'chat' && !confirmNavigation()) return;
    // [2026-06-01] Closing settings returns to chat without erasing the tab.
    // Why: preserving the tab makes a later Settings click reopen where the user
    // left off if a caller does not request a tab. How: only change viewMode.
    // Purpose: the navigation state stays predictable and compact.
    syncViewQuery('chat', get().activeSettingsTab, get().activeSettingsSection);
    set({ viewMode: 'chat' });
  }),

  setSettingsTab: commandAction('navigation.settingsTab', (requested: string, section?: string) => {
    const next = resolveSettingsLocation(requested, section);
    if ((get().viewMode !== 'settings' || next.tab !== get().activeSettingsTab) && !confirmNavigation()) return;
    // [2026-06-01] Tab changes are local to the settings view.
    // Why: adding future settings pages should not require App.tsx changes. How:
    // store only the tab id and let SettingsPageHost resolve it through the tab
    // registry. Purpose: tab routing remains data-driven.
    syncViewQuery('settings', next.tab, next.section);
    set({ activeSettingsTab: next.tab, activeSettingsSection: next.section, viewMode: 'settings' });
  }),
}));

// 老链接进来先把地址栏改写成新参数，不然刷新一次又走一遍迁移，且 ?domain= 一直挂着。
if (REQUESTED_VIEW === 'console' || (initialViewMode === 'settings' && (
  (query.get('tab') || DEFAULT_SETTINGS_TAB) !== initialLocation.tab
  || (query.get('section') || '') !== initialLocation.section
))) {
  syncViewQuery('settings', initialLocation.tab, initialLocation.section);
}

export { DEFAULT_SETTINGS_TAB };
