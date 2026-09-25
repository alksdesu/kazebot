import { MOUNT } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';

export interface FeatureRequestOptions {
  method?: string;
  body?: unknown;
  params?: Record<string, string | number | boolean | null | undefined>;
  scope?: string;
  signal?: AbortSignal;
}

export function featureUrl(path: string, options: Pick<FeatureRequestOptions, 'params' | 'scope'> = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(options.params || {})) {
    if (value !== null && value !== undefined && (value !== '' || key === 'bot_scope')) query.set(key, String(value));
  }
  if (options.scope) query.set('scope', options.scope);
  const suffix = query.toString();
  return `${MOUNT}${path}${suffix ? `?${suffix}` : ''}`;
}

export async function featureRequest<T>(path: string, options: FeatureRequestOptions = {}): Promise<T> {
  const token = useSettingsStore.getState().adminToken;
  const response = await fetch(featureUrl(path, options), {
    method: options.method || 'GET', signal: options.signal,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data.error || '请求失败');
    throw new Error(`${response.status} ${detail}`);
  }
  return response.json() as Promise<T>;
}

export async function downloadFeature(path: string, filename: string, scope?: string): Promise<void> {
  const token = useSettingsStore.getState().adminToken;
  const response = await fetch(featureUrl(path, { scope }), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) throw new Error(`下载失败：${response.status}`);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
