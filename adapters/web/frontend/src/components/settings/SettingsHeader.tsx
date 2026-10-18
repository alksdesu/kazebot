import { useEffect } from 'react';
import { DEFAULT_PLATFORM_ICON } from '../../console/pages';
import { useViewStore } from '../../store/viewStore';
import { Icon } from '../common';
import { useResolvedSettingsTab } from './settingsTabs';
import { browserPluginState } from '../../plugins/status';

export const SettingsHeader = () => {
  const activeSettingsTab = useViewStore(state => state.activeSettingsTab);
  const { tab, pending } = useResolvedSettingsTab(activeSettingsTab);
  // 平台清单没到就改写的话，深链到新平台的页会被冲成第一页。
  useEffect(() => {
    if (!pending && browserPluginState().settled && tab.id !== activeSettingsTab) useViewStore.getState().setSettingsTab(tab.id);
  }, [activeSettingsTab, pending, tab.id]);
  const icon = pending ? DEFAULT_PLATFORM_ICON : tab.icon || 'tune';
  const label = pending ? browserPluginState().settled ? '正在读取平台清单…' : '正在读取插件清单…' : tab.label;
  return <header className="app-header"><div className="min-w-0"><p className="mb-1 text-xs text-[var(--duties-secondary)]">设置</p><h2 className="flex min-w-0 items-center gap-2 text-base font-semibold"><Icon name={icon} size={19} /><span className="truncate">{label}</span></h2></div></header>;
};
