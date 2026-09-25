import { useViewStore } from '../../store/viewStore';
import { getSettingsTab } from './settingsTabs';

export const SettingsRightPanel = () => {
  const activeSettingsTab = useViewStore(state => state.activeSettingsTab);
  const RightPanel = getSettingsTab(activeSettingsTab).RightPanel;
  return RightPanel ? <RightPanel /> : null;
};
