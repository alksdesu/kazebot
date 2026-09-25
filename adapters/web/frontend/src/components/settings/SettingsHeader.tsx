import { useEffect } from 'react';
import { useViewStore } from '../../store/viewStore';
import { Icon } from '../common';
import { getSettingsTab } from './settingsTabs';

export const SettingsHeader = () => {
  const activeSettingsTab = useViewStore(state => state.activeSettingsTab);
  const tab = getSettingsTab(activeSettingsTab);
  useEffect(() => {
    if (tab.id !== activeSettingsTab) useViewStore.getState().setSettingsTab(tab.id);
  }, [activeSettingsTab, tab.id]);
  return <header className="app-header"><div className="min-w-0"><p className="mb-1 text-xs text-[var(--duties-secondary)]">设置</p><h2 className="flex min-w-0 items-center gap-2 text-base font-semibold"><Icon name={tab.icon || 'tune'} size={19} /><span className="truncate">{tab.label}</span></h2></div></header>;
};
