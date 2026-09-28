import { useEffect, useMemo, useState } from 'react';

import { searchableSections, type SearchableSection } from '../../console/schemaSearch';
import { picksSection } from '../../console/sectionPick';
import { useSchemaStore } from '../../console/schemaStore';
import { useSettingsStore } from '../../store/settingsStore';
import { WorkspaceNav } from '../../features/WorkspaceNav';
import { useViewStore } from '../../store/viewStore';
import { Icon } from '../common';
import { SETTINGS_GROUP_LABELS, SETTINGS_GROUP_ORDER, useResolvedSettingsTab, useSettingsTabs, type SettingsTabDefinition } from './settingsTabs';

interface VisibleTab {
  tab: SettingsTabDefinition;
  /** 搜索命中的分区。不搜索时为空 —— 平铺九个分区会把侧栏挤满。 */
  sections: readonly SearchableSection[];
}

export const SettingsSidebar = () => {
  const isConnected = useSettingsStore(state => state.isConnected);
  const token = useSettingsStore(state => state.adminToken);
  const schemas = useSchemaStore(state => state.schemas);
  const loadSchema = useSchemaStore(state => state.load);
  const { activeSettingsTab, closeSettings, setSettingsTab, viewMode } = useViewStore();
  const [search, setSearch] = useState('');
  const tabs = useSettingsTabs();
  const resolved = useResolvedSettingsTab(activeSettingsTab);
  const selectedTab = resolved.pending ? '' : resolved.tab.id;
  // 分区和字段名只在 schema 里，一登录就把各页的拉下来，没打开过的页也搜得到。
  useEffect(() => {
    if (!token) return;
    for (const tab of tabs) {
      if (tab.configScope) void loadSchema(token, tab.configScope);
    }
  }, [loadSchema, tabs, token]);
  const visible = useMemo<VisibleTab[]>(() => {
    const query = search.trim().toLocaleLowerCase();
    return tabs.flatMap(tab => {
      const schema = tab.configScope ? schemas[tab.configScope.id] : undefined;
      const sections = query && schema
        ? searchableSections(schema).filter(section => picksSection(tab.sections, section.id) && section.text.includes(query))
        : [];
      const tabHit = `${tab.label} ${SETTINGS_GROUP_LABELS[tab.group || 'basics']} ${tab.id}`.toLocaleLowerCase().includes(query);
      return tabHit || sections.length ? [{ tab, sections }] : [];
    });
  }, [schemas, search, tabs]);
  return <div className="flex h-full min-h-0 flex-col">
    <div className="border-b border-[var(--duties-border)] p-4">
      <button className="mb-3 inline-flex min-h-10 items-center gap-2 text-sm text-[var(--duties-secondary)] hover:text-[var(--duties-text)]" onClick={closeSettings} type="button"><Icon name="arrow_back" size={18} />返回聊天</button>
      <div className="flex items-center gap-3"><img src={`${import.meta.env.BASE_URL}logo-sm.jpg`} alt="Clonoth" className="h-9 w-9 rounded-lg" /><div><h1 className="text-base font-semibold">{viewMode === 'settings' ? '设置' : '工作区'}</h1><p className="mt-0.5 text-xs text-[var(--duties-tertiary)]">Clonoth 控制台</p></div></div>
    </div>
    <WorkspaceNav />
    <div className="px-3 pb-2 pt-3"><label className="sr-only" htmlFor="settings-search">搜索设置</label><input id="settings-search" type="search" className="app-input w-full" placeholder="搜索设置" value={search} onChange={event => setSearch(event.target.value)} /></div>
    <nav aria-label="设置分区" className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
      {!visible.length && <p className="p-3 text-sm text-[var(--duties-secondary)]" role="status">没有匹配的设置，试试“模型”或“群”。</p>}
      {SETTINGS_GROUP_ORDER.map(group => {
        const tabs = visible.filter(entry => (entry.tab.group || 'basics') === group);
        if (!tabs.length) return null;
        return <div className="mb-3" key={group}>
          {SETTINGS_GROUP_LABELS[group] && <p className="px-3 pb-2 pt-3 text-xs font-semibold text-[var(--duties-tertiary)]">{SETTINGS_GROUP_LABELS[group]}</p>}
          {tabs.map(({ tab, sections }) => {
            const active = viewMode === 'settings' && tab.id === selectedTab;
            // 和页同名的分区（表情包图库页只摆那一块）并进页那一行，同一个名字不上下列两遍。
            const merged = sections.find(section => section.label === tab.label);
            const rest = merged ? sections.filter(section => section !== merged) : sections;
            return <div key={tab.id}>
              <button aria-current={active ? 'page' : undefined} aria-label={tab.label} className={`app-nav-item ${active ? 'app-nav-item-active' : ''}`} onClick={() => setSettingsTab(tab.id, merged?.id)} type="button">
                {tab.icon && <Icon name={tab.icon} size={18} />}<span>{tab.label}</span>
              </button>
              {rest.map(section => (
                <button
                  aria-label={`${tab.label}：${section.label}`}
                  className="app-nav-item pl-9 text-xs"
                  key={section.id}
                  onClick={() => setSettingsTab(tab.id, section.id)}
                  type="button"
                >
                  <span>{section.label}</span>
                </button>
              ))}
            </div>;
          })}
        </div>;
      })}
    </nav>
    <div className="border-t border-[var(--duties-border)] px-4 py-3"><p className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]"><span aria-hidden="true" className={`h-2 w-2 rounded-full ${isConnected ? 'bg-[var(--duties-live)]' : 'bg-[var(--duties-danger)]'}`} />{isConnected ? '已连接' : '已断开'}</p></div>
  </div>;
};
