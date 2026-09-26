// 改版前的页签 id。书签、聊天记录和实例跳转链接里还带着它们，不认就会落到「通用」。
// 这个模块不许有 import：viewStore 和页签注册表都要用它，任何依赖都会绕回来成环。
const TAB_ALIASES: Record<string, string> = {
  'qq-persona': 'persona',
  'qq-memory': 'memory',
  'qq-stickers': 'stickers',
  'qq-community': 'community',
  'qq-conversation': 'conversation',
  'qq-providers': 'providers',
  'qq-models': 'models',
  'qq-timing': 'qq-trigger',
};

export function resolveSettingsTabId(tabId: string): string {
  return TAB_ALIASES[tabId] || tabId;
}

export { TAB_ALIASES };
