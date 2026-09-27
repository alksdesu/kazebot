// 拿后端注册表换掉内置的那份平台描述。会话种类、策略作用域校验都按它判，
// 拉不到就继续用内置的 —— 认不出通道会让整张会话列表退化成「会话」。
import { useEffect, useSyncExternalStore } from 'react';

import { getPlatforms, type PlatformSummary } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import {
  adoptPlatforms,
  platformRegistryState,
  settlePlatforms,
  subscribePlatforms,
  type ChannelKind,
  type PlatformDescriptor,
  type PlatformRegistryState,
} from './registry';

const KINDS: ReadonlySet<string> = new Set(['group', 'private', 'bridge', 'other']);

export function toDescriptor(row: PlatformSummary): PlatformDescriptor | null {
  if (!row?.id || typeof row.id !== 'string') return null;
  return {
    id: row.id,
    displayName: row.display_name || row.id,
    shortName: row.short_name || row.id,
    channels: (Array.isArray(row.channels) ? row.channels : [])
      .filter((channel) => channel?.name && KINDS.has(channel.kind))
      .map((channel) => ({ name: channel.name, kind: channel.kind as ChannelKind, hashed: channel.hashed !== false })),
    hasConfig: Boolean(row.has_config),
    entrypoints: (Array.isArray(row.entrypoints) ? row.entrypoints : [])
      .filter((entry): entry is string => typeof entry === 'string' && entry.length > 0),
    connectionPeer: typeof row.connection_peer === 'string' ? row.connection_peer : '',
    connectionHint: typeof row.connection_hint === 'string' ? row.connection_hint : '',
  };
}

export function usePlatformRegistry(): void {
  const token = useSettingsStore((state) => state.adminToken);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    void getPlatforms(token)
      .then((rows) => {
        if (!alive) return;
        adoptPlatforms(rows.map(toDescriptor).filter((row): row is PlatformDescriptor => row !== null));
      })
      .catch(() => {
        if (alive) settlePlatforms();
      });
    return () => { alive = false; };
  }, [token]);
}

/** 平台清单和它到没到。清单换了才重画。 */
export function usePlatformRegistryState(): PlatformRegistryState {
  return useSyncExternalStore(subscribePlatforms, platformRegistryState);
}
