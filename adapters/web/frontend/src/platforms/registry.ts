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
};

const BUILTIN_PLATFORMS: readonly PlatformDescriptor[] = [QQ_NAPCAT];

let platforms: readonly PlatformDescriptor[] = BUILTIN_PLATFORMS;
let channelIndex = buildIndex(platforms);

function buildIndex(rows: readonly PlatformDescriptor[]): Map<string, { platform: PlatformDescriptor; channel: ChannelDescriptor }> {
  const index = new Map<string, { platform: PlatformDescriptor; channel: ChannelDescriptor }>();
  for (const platform of rows) {
    for (const channel of platform.channels) index.set(channel.name, { platform, channel });
  }
  return index;
}

export function allPlatforms(): readonly PlatformDescriptor[] {
  return platforms;
}

export function getPlatform(id: string): PlatformDescriptor | null {
  return platforms.find((platform) => platform.id === id) || null;
}

/** 换上后端公布的清单。空清单一律忽略：认不出通道会让整张会话列表退化成「会话」。 */
export function adoptPlatforms(rows: readonly PlatformDescriptor[]): void {
  if (!rows.length) return;
  platforms = rows;
  channelIndex = buildIndex(platforms);
}

export function resetPlatforms(): void {
  platforms = BUILTIN_PLATFORMS;
  channelIndex = buildIndex(platforms);
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
