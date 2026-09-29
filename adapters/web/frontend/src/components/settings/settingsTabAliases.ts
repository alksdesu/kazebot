// 改版前的页签 id。书签、聊天记录和实例跳转链接里还带着它们，不认就会落到「通用」。
// 这个模块不许有 import：viewStore 和页签注册表都要用它，任何依赖都会绕回来成环。
export interface SettingsLocation {
  tab: string;
  /** 平台页里的分区。空串表示不指定，由页面自己决定展开哪些。 */
  section: string;
}

const TAB_ALIASES: Record<string, SettingsLocation> = {
  'qq-persona': { tab: 'persona', section: '' },
  'qq-memory': { tab: 'memory', section: '' },
  'qq-stickers': { tab: 'stickers', section: '' },
  'qq-community': { tab: 'community', section: '' },
  'qq-conversation': { tab: 'conversation', section: '' },
  'qq-providers': { tab: 'providers', section: '' },
  'qq-models': { tab: 'models', section: '' },
  // QQ 的九页收进一个平台页，各自成了分区。
  'qq-account': { tab: 'platform-qq', section: 'account' },
  'qq-channels': { tab: 'platform-qq', section: 'channels' },
  'qq-timing': { tab: 'platform-qq', section: 'trigger' },
  'qq-trigger': { tab: 'platform-qq', section: 'trigger' },
  'qq-permissions': { tab: 'platform-qq', section: 'permissions' },
  'qq-input': { tab: 'platform-qq', section: 'input' },
  'qq-output': { tab: 'platform-qq', section: 'output' },
  'qq-interaction': { tab: 'platform-qq', section: 'interaction' },
  'qq-sticker': { tab: 'platform-qq', section: 'sticker' },
  'qq-link': { tab: 'platform-qq', section: 'link' },
};

/** 旧 id 自带分区，显式传的分区优先 —— 深链已经指名要哪一块了。 */
export function resolveSettingsLocation(tabId: string, section = ''): SettingsLocation {
  const alias = TAB_ALIASES[tabId];
  const location = { tab: alias?.tab || tabId, section: section || alias?.section || '' };
  if (location.tab === 'platform-qq' && location.section === 'account') {
    return { tab: 'general', section: 'account' };
  }
  return location;
}

export function resolveSettingsTabId(tabId: string): string {
  return resolveSettingsLocation(tabId).tab;
}

export { TAB_ALIASES };
