// 会话的请求最终用哪个模型、哪个地址、走哪个渠道，和 engine/model.py 的 resolve_provider 同一个顺序：
// 会话覆盖 → 节点 → 两者点名的渠道块 → 全局在用渠道。哪一层还没读到就停在那一层，不拿下一层顶上。
import type { ProviderConfigPublic } from '../api/supervisorClient';

export type ReadStatus = 'loaded' | 'loading' | 'failed';

/**
 * 继承链上一层的状态。missing：读成了，但里面没有要找的那一行（会话的节点不在节点表里）。
 * 它不算读失败，也同样停在这一层：engine 加载不到这个节点，任务直接失败，下一层的值用不上。
 */
export type LayerStatus = ReadStatus | 'missing';

/** 共用数据读到哪了：读成过就算读到，没读成过时有原因是读失败，没有是还在读。 */
export function readStatus(read: { readonly loaded: boolean; readonly error: string }): ReadStatus {
  if (read.loaded) return 'loaded';
  return read.error ? 'failed' : 'loading';
}

export type InheritedSource = 'session' | 'node' | 'channel' | 'global';

/** 继承链上的一层。value 为空就往下一层找；origin 是这一层是谁（节点 id、渠道名），给来源标签用。 */
export interface InheritedLayer {
  readonly source: InheritedSource;
  readonly status: LayerStatus;
  readonly value: string;
  readonly origin?: string;
}

export type Inherited =
  | { readonly known: true; readonly source: InheritedSource | 'default'; readonly value: string; readonly origin: string }
  | { readonly known: false; readonly source: InheritedSource; readonly status: Exclude<LayerStatus, 'loaded'>; readonly origin: string };

export function resolveInherited(layers: readonly InheritedLayer[]): Inherited {
  for (const layer of layers) {
    const origin = layer.origin || '';
    if (layer.status !== 'loaded') return { known: false, source: layer.source, status: layer.status, origin };
    if (layer.value) return { known: true, source: layer.source, value: layer.value, origin };
  }
  return { known: true, source: 'default', value: '', origin: '' };
}

/** 拼继承链要的四份数据。status 不是 loaded 时另外几个字段不看。 */
export interface InheritanceSources {
  /** 会话覆盖。会话面板里是编辑框里将要存下的那份。 */
  readonly session: { readonly status: ReadStatus; readonly fields: object | null };
  /** 会话当前节点在节点表里的那一行；id 是那一行的 id，表里没有时是会话记着的那个。 */
  readonly node: { readonly status: LayerStatus; readonly id: string; readonly fields: object | null };
  /** 渠道表：渠道名 → 公开配置，以及表里记的在用渠道。 */
  readonly channels: {
    readonly status: ReadStatus;
    readonly table: Readonly<Record<string, Partial<ProviderConfigPublic>>> | null;
    readonly active: string;
  };
  /** 全局在用渠道的模型和地址。 */
  readonly global: { readonly status: ReadStatus; readonly model: string; readonly baseUrl: string };
}

// 覆盖、节点行、渠道块都是接口原样回来的 JSON，字段不一定是字符串
const text = (fields: object | null | undefined, key: string): string => {
  const value = fields ? (fields as Record<string, unknown>)[key] : undefined;
  return typeof value === 'string' ? value.trim() : '';
};

// engine 读会话覆盖时 provider 和旧写法 provider_type 都认，节点只认 provider
const sessionProvider = (fields: object | null): string => text(fields, 'provider') || text(fields, 'provider_type');

/** 渠道块那一层：会话点名的渠道优先，没点名才看节点点名的；名字底下没有块时按线格式名处理，这一层什么也不给。 */
function channelLayer(sources: InheritanceSources, field: 'model' | 'base_url'): InheritedLayer {
  const { session, node, channels } = sources;
  if (session.status !== 'loaded') return { source: 'channel', status: session.status, value: '' };
  let name = sessionProvider(session.fields);
  if (!name) {
    if (node.status !== 'loaded') return { source: 'channel', status: node.status, value: '' };
    name = text(node.fields, 'provider');
  }
  if (!name) return { source: 'channel', status: 'loaded', value: '' };
  if (channels.status !== 'loaded') return { source: 'channel', status: channels.status, value: '', origin: name };
  return { source: 'channel', status: 'loaded', value: text(channels.table?.[name], field), origin: name };
}

/**
 * field 在这个会话上最终取到的值。withSession 为 false 时不看会话自己填的这一项（输入框留空会继承到什么），
 * 会话点名的渠道仍然算数：渠道块那一层认的是它。
 */
export function inheritedField(sources: InheritanceSources, field: 'model' | 'base_url', withSession = true): Inherited {
  const { session, node, global } = sources;
  const layers: InheritedLayer[] = [];
  if (withSession) layers.push({ source: 'session', status: session.status, value: text(session.fields, field) });
  layers.push({ source: 'node', status: node.status, value: text(node.fields, field), origin: node.id });
  layers.push(channelLayer(sources, field));
  layers.push({ source: 'global', status: global.status, value: field === 'model' ? global.model.trim() : global.baseUrl.trim() });
  return resolveInherited(layers);
}

/** 这个会话的请求走哪个渠道：会话点名的、节点点名的，都没点名就是在用的那个。 */
export function inheritedProvider(sources: InheritanceSources, withSession = true): Inherited {
  const { session, node, channels } = sources;
  const layers: InheritedLayer[] = [];
  if (withSession) layers.push({ source: 'session', status: session.status, value: sessionProvider(session.fields) });
  layers.push({ source: 'node', status: node.status, value: text(node.fields, 'provider'), origin: node.id });
  layers.push({ source: 'global', status: channels.status, value: channels.active.trim() });
  return resolveInherited(layers);
}
