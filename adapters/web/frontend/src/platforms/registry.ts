// 平台描述的前端副本，字段对着 platforms/descriptor.py 的 public_dict()。
// 随包内置一份，界面在拿到 /v1/platforms 之前也能判会话种类；拿到之后再覆盖。
export type ChannelKind = 'group' | 'private' | 'bridge' | 'other';

/** 真人对话的两种会话。bridge 是转发桥这类系统会话，other 是认不出种类时的兜底前缀。 */
const CONVERSATION_KINDS: ReadonlySet<ChannelKind> = new Set<ChannelKind>(['group', 'private']);

export interface ChannelDescriptor {
  /** 会话通道名，同时是会话键前缀。 */
  name: string;
  kind: ChannelKind;
  /** 会话键是否为「前缀:摘要」。 */
  hashed: boolean;
}

export interface PlatformDescriptor {
  id: string;
  displayName: string;
  /** 称呼这个平台的用户时用的前缀，比如提醒署名「QQ 12345」。 */
  shortName: string;
  channels: ChannelDescriptor[];
  hasConfig: boolean;
  /** 平台进程的入口脚本（相对工作区），提示人去服务器上找哪个进程时用。 */
  entrypoints: string[];
  /** 适配进程要连上的那一端，连接卡片上这么称呼它；空串时叫「平台」。 */
  connectionPeer: string;
  /** 连不上时让人先查什么；空串就不提示。 */
  connectionHint: string;
}

export const QQ_NAPCAT: PlatformDescriptor = {
  id: 'qq',
  displayName: 'QQ · NapCat',
  shortName: 'QQ',
  channels: [
    { name: 'qq_group', kind: 'group', hashed: true },
    { name: 'qq_private', kind: 'private', hashed: true },
    { name: 'qq_forward', kind: 'bridge', hashed: false },
    { name: 'qq_unknown', kind: 'other', hashed: true },
  ],
  hasConfig: true,
  entrypoints: ['bot.py'],
  connectionPeer: 'NapCat',
  connectionHint: '检查 NapCat 登录与反向 WebSocket 地址',
};

const BUILTIN_PLATFORMS: readonly PlatformDescriptor[] = [QQ_NAPCAT];

export interface PlatformRegistryState {
  platforms: readonly PlatformDescriptor[];
  /** 后端的清单到了，或者确定拿不到了。到之前认不出的平台页，可能只是清单还在路上。 */
  settled: boolean;
}

let state: PlatformRegistryState = { platforms: BUILTIN_PLATFORMS, settled: false };
let channelIndex = buildIndex(state.platforms);
const listeners = new Set<() => void>();

// 每次整份换掉：订阅方按引用比，引用没变就是没变。
function publish(next: PlatformRegistryState): void {
  state = next;
  channelIndex = buildIndex(next.platforms);
  for (const listener of listeners) listener();
}

function buildIndex(rows: readonly PlatformDescriptor[]): Map<string, { platform: PlatformDescriptor; channel: ChannelDescriptor }> {
  const index = new Map<string, { platform: PlatformDescriptor; channel: ChannelDescriptor }>();
  for (const platform of rows) {
    for (const channel of platform.channels) index.set(channel.name, { platform, channel });
  }
  return index;
}

export function allPlatforms(): readonly PlatformDescriptor[] {
  return state.platforms;
}

export function getPlatform(id: string): PlatformDescriptor | null {
  return state.platforms.find((platform) => platform.id === id) || null;
}

export function platformRegistryState(): PlatformRegistryState {
  return state;
}

export function subscribePlatforms(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** 换上后端公布的清单。空清单一律忽略：认不出通道会让整张会话列表退化成「会话」。 */
export function adoptPlatforms(rows: readonly PlatformDescriptor[]): void {
  if (!rows.length) {
    settlePlatforms();
    return;
  }
  publish({ platforms: rows, settled: true });
}

/** 清单拿不到：接着用内置的那份，但不用再等了。 */
export function settlePlatforms(): void {
  if (!state.settled) publish({ ...state, settled: true });
}

export function resetPlatforms(): void {
  publish({ platforms: BUILTIN_PLATFORMS, settled: false });
}

/** 接在「适配器进程」后面的入口脚本，写成「（bot.py）」；平台没声明时是空串。 */
export function entrypointNote(platform: PlatformDescriptor): string {
  return platform.entrypoints.length ? `（${platform.entrypoints.join('、')}）` : '';
}

export function connectionPeerName(platform: PlatformDescriptor): string {
  return platform.connectionPeer || '平台';
}

/** 会话键前缀 → 平台与通道。认不出返回 null，调用方据此退到 owner 上报的种类。
 *
 * 光秃秃一个前缀不算会话键：策略作用域校验靠它挡住 `qq_group:` 这种空目标。 */
export function channelForScope(scope: string): { platform: PlatformDescriptor; channel: ChannelDescriptor } | null {
  const separator = scope.indexOf(':');
  if (separator <= 0 || separator === scope.length - 1) return null;
  return channelIndex.get(scope.slice(0, separator)) || null;
}

export function platformForScope(scope: string): PlatformDescriptor | null {
  return channelForScope(scope)?.platform || null;
}

export type ConversationKind = 'group' | 'private';

/** 这个会话键是群还是私聊。转发桥、兜底通道和非平台会话都返回 null。 */
export function conversationKindForScope(scope: string): ConversationKind | null {
  const kind = channelForScope(scope)?.channel.kind;
  return kind && CONVERSATION_KINDS.has(kind) ? (kind as ConversationKind) : null;
}

/** 是不是一条真人对话的会话键。策略作用域、群操作目标都只认这种。 */
export function isConversationScope(scope: string): boolean {
  return conversationKindForScope(scope) !== null;
}

/** 这串文本本身就是个会话键前缀开头的技术标识，不是人看的名字。 */
export function looksLikeScopeKey(value: string): boolean {
  const separator = value.indexOf(':');
  return separator > 0 && channelIndex.has(value.slice(0, separator));
}
