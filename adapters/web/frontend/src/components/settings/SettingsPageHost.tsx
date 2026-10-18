// [2026-06-01] Main content host for registered settings tabs.
// Why: App.tsx should not know which concrete settings page is selected. How: read
// activeSettingsTab from viewStore and resolve the component through settingsTabs.
// Purpose: settings pages can be added, removed, or reordered without changing the
// root application component.
import { useViewStore } from '../../store/viewStore';
import { Empty } from './pages/settingsControls';
import { builtinSettingsTab, useResolvedSettingsTab } from './settingsTabs';
import { PluginRender } from '../../plugins/PluginRender';
import type { SettingsTabDefinition } from './settingsTabs';
import { browserPluginState } from '../../plugins/status';

export const SettingsPageHost = () => {
  const activeSettingsTab = useViewStore(state => state.activeSettingsTab);
  const { tab, pending } = useResolvedSettingsTab(activeSettingsTab);
  // 深链到的平台页等清单到了再画，免得先闪一下第一页。
  if (pending) return <Empty>{browserPluginState().settled ? '正在读取平台清单…' : '正在读取插件清单…'}</Empty>;
  return <PluginRender<SettingsTabDefinition> kind="settings" id={tab.id} base={builtinSettingsTab(tab.id)} render={({ Page }) => <Page />} />;
};
