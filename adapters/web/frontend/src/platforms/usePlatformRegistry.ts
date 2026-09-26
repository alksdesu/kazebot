// 拿后端注册表换掉内置的那份平台描述。会话种类、策略作用域校验都按它判，
// 拉不到就继续用内置的 —— 认不出通道会让整张会话列表退化成「会话」。
import { useEffect } from 'react';

import { getPlatforms, type PlatformSummary } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { adoptPlatforms, type ChannelKind, type PlatformDescriptor } from './registry';

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
      .catch(() => undefined);
    return () => { alive = false; };
  }, [token]);
}
