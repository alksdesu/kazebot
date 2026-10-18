import { useViewStore } from '../../store/viewStore';
import { builtinSettingsTab, useResolvedSettingsTab } from './settingsTabs';
import { PluginRender } from '../../plugins/PluginRender';
import type { SettingsTabDefinition } from './settingsTabs';

export const SettingsRightPanel = () => {
  const activeSettingsTab = useViewStore(state => state.activeSettingsTab);
  const { tab, pending } = useResolvedSettingsTab(activeSettingsTab);
  return pending ? null : <PluginRender<SettingsTabDefinition> kind="settings" id={tab.id} base={builtinSettingsTab(tab.id)} render={({ RightPanel }) => RightPanel ? <RightPanel /> : null} />;
};
