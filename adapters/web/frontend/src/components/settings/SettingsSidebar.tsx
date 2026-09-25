import { useMemo, useState } from 'react';

import { useSettingsStore } from '../../store/settingsStore';
import { WorkspaceNav } from '../../features/WorkspaceNav';
import { useViewStore } from '../../store/viewStore';
import { Icon } from '../common';
import { getSettingsTab, SETTINGS_GROUP_LABELS, SETTINGS_GROUP_ORDER, settingsTabs } from './settingsTabs';

export const SettingsSidebar = () => {
  const isConnected = useSettingsStore(state => state.isConnected);
  const { activeSettingsTab, closeSettings, setSettingsTab, viewMode } = useViewStore();
  const [search, setSearch] = useState('');
  const selectedTab = getSettingsTab(activeSettingsTab).id;
  const visible = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return settingsTabs.filter(tab => `${tab.label} ${SETTINGS_GROUP_LABELS[tab.group || 'basics']} ${tab.id}`.toLocaleLowerCase().includes(query));
  }, [search]);
  return <div className="flex h-full min-h-0 flex-col">
    <div className="border-b border-[var(--duties-border)] p-4">
      <button className="mb-3 inline-flex min-h-10 items-center gap-2 text-sm text-[var(--duties-secondary)] hover:text-[var(--duties-text)]" onClick={closeSettings} type="button"><Icon name="arrow_back" size={18} />返回聊天</button>
      <div className="flex items-center gap-3"><img src={`${import.meta.env.BASE_URL}logo-sm.jpg`} alt="Clonoth" className="h-9 w-9 rounded-lg" /><div><h1 className="text-base font-semibold">{viewMode === 'settings' ? '设置' : '工作区'}</h1><p className="mt-0.5 text-xs text-[var(--duties-tertiary)]">QQ Bot 控制台</p></div></div>
    </div>
    <WorkspaceNav />
    <div className="px-3 pb-2 pt-3"><label className="sr-only" htmlFor="settings-search">搜索设置</label><input id="settings-search" type="search" className="app-input w-full" placeholder="搜索设置" value={search} onChange={event => setSearch(event.target.value)} /></div>
    <nav aria-label="设置分区" className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
      {!visible.length && <p className="p-3 text-sm text-[var(--duties-secondary)]" role="status">没有匹配的设置，试试“模型”或“群”。</p>}
      {SETTINGS_GROUP_ORDER.map(group => {
        const tabs = visible.filter(tab => (tab.group || 'basics') === group);
        if (!tabs.length) return null;
        return <div className="mb-3" key={group}>
          {SETTINGS_GROUP_LABELS[group] && <p className="px-3 pb-2 pt-3 text-xs font-semibold text-[var(--duties-tertiary)]">{SETTINGS_GROUP_LABELS[group]}</p>}
          {tabs.map(tab => {
            const active = viewMode === 'settings' && tab.id === selectedTab;
            return <button aria-current={active ? 'page' : undefined} aria-label={tab.label} className={`app-nav-item ${active ? 'app-nav-item-active' : ''}`} key={tab.id} onClick={() => setSettingsTab(tab.id)} type="button">
              {tab.icon && <Icon name={tab.icon} size={18} />}<span>{tab.label}</span>
            </button>;
          })}
        </div>;
      })}
    </nav>
    <div className="border-t border-[var(--duties-border)] px-4 py-3"><p className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]"><span aria-hidden="true" className={`h-2 w-2 rounded-full ${isConnected ? 'bg-[var(--duties-live)]' : 'bg-[var(--duties-danger)]'}`} />{isConnected ? '已连接' : '已断开'}</p></div>
  </div>;
};
