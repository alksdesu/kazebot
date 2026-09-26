// [2026-06-01] Settings tab registry for the full settings view.
// Why: adding settings pages should be a registration change, not another App.tsx
// conditional. How: each tab provides a label, order, main Page, and optional right
// panel component. Purpose: the settings sidebar, host, and right panel all resolve
// pages from the same data source.
import { type ComponentType } from 'react';
import { CommunityPage } from '../../features/community/CommunityPage';
import { ConversationSettings } from '../../features/community/ConversationSettings';
import { RemindersPage } from '../../features/execution/RemindersPage';

import {
  BOT_PAGES,
  MODEL_PAGES,
  QQ_SECTIONS,
  wrapConsolePage,
  wrapPlatformSection,
} from '../../console/pages';
import { resolveSettingsTabId } from './settingsTabAliases';
import { ClientSettingsPage } from './pages/ClientSettingsPage';
import { GeneralSettingsPage } from './pages/GeneralSettingsPage';
import { NodeFilesSettingsPage } from './pages/NodeFilesSettingsPage';
import { AdvancedSettingsPage } from './pages/AdvancedSettingsPage';
import { AgentsSettingsPage } from './pages/AgentsSettingsPage';
import { ApprovalsSettingsPage } from './pages/ApprovalsSettingsPage';
import { AutomationSettingsPage } from './pages/AutomationSettingsPage';
import { McpSettingsPage } from './pages/McpSettingsPage';
import { SkillsSettingsPage } from './pages/SkillsSettingsPage';
import { SystemSettingsPage } from './pages/SystemSettingsPage';
import { ToolsSettingsPage } from './pages/ToolsSettingsPage';
import { DrawtoolsSettingsPage } from './pages/DrawtoolsSettingsPage';
import { RuntimeSettingsPage } from './pages/RuntimeSettingsPage';
import {
  AdvancedSettingsRightPanel,
  AgentsSettingsRightPanel,
  ApprovalsSettingsRightPanel,
  AutomationSettingsRightPanel,
  McpSettingsRightPanel,
  SkillsSettingsRightPanel,
  SystemSettingsRightPanel,
  ToolsSettingsRightPanel,
} from './panels/SettingsContextPanels';

// 侧栏分段。合并控制台之后这里有二十多项，一长条平铺没法找。
// 机器人是「它是谁、记得什么」，平台是「它在哪、怎么收发」，引擎是「它怎么跑」。
export type SettingsGroup = 'basics' | 'bot' | 'models' | 'engine' | 'platform';

export const SETTINGS_GROUP_LABELS: Record<SettingsGroup, string> = {
  basics: '',
  bot: '机器人',
  models: '模型与渠道',
  engine: '引擎',
  platform: '平台',
};

export const SETTINGS_GROUP_ORDER: SettingsGroup[] = ['basics', 'bot', 'models', 'engine', 'platform'];

export interface SettingsTabDefinition {
  id: string;
  label: string;
  group?: SettingsGroup;
  // [2026-06-01] Why: settings icon values used to be literal Unicode glyphs.
  // How: keep the field as a string but store Material Symbol names instead.
  // Purpose: renderers can pass the value directly to the shared Icon component.
  icon?: string;
  order: number;
  Page: ComponentType;
  RightPanel?: ComponentType;
}

const CORE_TABS = [
  { id: 'general', label: '通用', icon: 'tune', group: 'basics', order: 0, Page: GeneralSettingsPage },
  // [2026-06-01] Register browser-only preferences as a first-class settings tab.
  // Why: auto-approval and render defaults are local frontend choices, not backend
  // policy. How: point the new Client tab to ClientSettingsPage. Purpose: future
  // client preferences can be added without editing App.tsx or settings hosts.
  { id: 'client', label: '客户端', icon: 'display_settings', group: 'basics', order: 1, Page: ClientSettingsPage },
  // [2026-06-02] Register the full P0/P1 settings surface requested by operators.
  // Why: system, approvals, agents, tools, skills, MCP, automation, and advanced raw
  // config are independent settings domains. How: add each page with a Material
  // Symbol icon, explicit order, and contextual right panel. Purpose: future settings
  // navigation remains data-driven through this one registry.
  { id: 'system', label: '系统', icon: 'settings_power', group: 'engine', order: 4, Page: SystemSettingsPage, RightPanel: SystemSettingsRightPanel },
  { id: 'approvals', label: '审批', icon: 'approval', group: 'engine', order: 5, Page: ApprovalsSettingsPage, RightPanel: ApprovalsSettingsRightPanel },
  { id: 'agents', label: '节点', icon: 'smart_toy', group: 'engine', order: 6, Page: AgentsSettingsPage, RightPanel: AgentsSettingsRightPanel },
  { id: 'node-files', label: '节点文件', icon: 'folder_managed', group: 'engine', order: 7, Page: NodeFilesSettingsPage },
  { id: 'tools', label: '工具与权限', icon: 'build', group: 'engine', order: 8, Page: ToolsSettingsPage, RightPanel: ToolsSettingsRightPanel },
  { id: 'drawtools', label: '绘图', icon: 'palette', group: 'engine', order: 9, Page: DrawtoolsSettingsPage },
  { id: 'skills', label: '技能', icon: 'menu_book', group: 'engine', order: 10, Page: SkillsSettingsPage, RightPanel: SkillsSettingsRightPanel },
  { id: 'mcp', label: 'MCP', icon: 'cable', group: 'engine', order: 11, Page: McpSettingsPage, RightPanel: McpSettingsRightPanel },
  { id: 'automation', label: '自动化', icon: 'schedule', group: 'engine', order: 12, Page: AutomationSettingsPage, RightPanel: AutomationSettingsRightPanel },
  { id: 'advanced', label: '高级', icon: 'code', group: 'engine', order: 13, Page: AdvancedSettingsPage, RightPanel: AdvancedSettingsRightPanel },
  // 出过一次事：engine 崩了三个半小时没人发现，因为界面上没有任何地方看得到它。
  { id: 'runtime', label: '运行', icon: 'monitor_heart', group: 'engine', order: 3, Page: RuntimeSettingsPage },
  // 提醒是人在聊天里建的，和 cron 定时任务两回事，不该藏在自动化里。
  { id: 'reminders', label: '提醒', icon: 'notifications_active', group: 'bot', order: 25, Page: RemindersPage },
  { id: 'community', label: '群协作', icon: 'groups', group: 'bot', order: 23, Page: CommunityPage },
  { id: 'conversation', label: '会话与接话', icon: 'forum', group: 'bot', order: 24, Page: ConversationSettings },
] satisfies SettingsTabDefinition[];

const BOT_TABS: SettingsTabDefinition[] = BOT_PAGES.map((page, index) => ({
  id: page.id,
  label: page.label,
  icon: page.icon,
  group: 'bot',
  order: 20 + index,
  Page: wrapConsolePage(SETTINGS_GROUP_LABELS.bot, page),
}));

const MODEL_TABS: SettingsTabDefinition[] = MODEL_PAGES.map((page, index) => ({
  id: page.id,
  label: page.label,
  icon: page.icon,
  group: 'models',
  order: 30 + index,
  Page: wrapConsolePage(SETTINGS_GROUP_LABELS.models, page),
}));

const PLATFORM_TABS: SettingsTabDefinition[] = QQ_SECTIONS.map((section, index) => ({
  id: section.id,
  label: section.label,
  icon: section.icon,
  group: 'platform',
  order: 40 + index,
  Page: wrapPlatformSection(section),
}));

export const settingsTabs: SettingsTabDefinition[] = [...CORE_TABS, ...BOT_TABS, ...MODEL_TABS, ...PLATFORM_TABS]
  .sort((a, b) => a.order - b.order);

export function getSettingsTab(tabId: string): SettingsTabDefinition {
  // [2026-06-01] Unknown tab ids fall back to the first registered settings page.
  // Why: stale links or future removed tabs should not blank the settings view. How:
  // resolve through the registry and return settingsTabs[0] as a safe default.
  // Purpose: the host and right panel share identical fallback behavior.
  const resolved = resolveSettingsTabId(tabId);
  return settingsTabs.find(tab => tab.id === resolved) || settingsTabs[0];
}
