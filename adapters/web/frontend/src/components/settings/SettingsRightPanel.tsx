import { useViewStore } from '../../store/viewStore';
import { useResolvedSettingsTab } from './settingsTabs';

export const SettingsRightPanel = () => {
  const activeSettingsTab = useViewStore(state => state.activeSettingsTab);
  const { tab, pending } = useResolvedSettingsTab(activeSettingsTab);
  const RightPanel = pending ? undefined : tab.RightPanel;
  return RightPanel ? <RightPanel /> : null;
};
