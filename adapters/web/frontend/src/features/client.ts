import { downloadFile } from '../api/blobs';
import { requestJson } from '../api/request';
import { MOUNT } from '../instances/paths';

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

export function featureRequest<T>(path: string, options: FeatureRequestOptions = {}): Promise<T> {
  return requestJson<T>(featureUrl(path, options), {
    method: options.method || 'GET',
    signal: options.signal,
    ...(options.body === undefined ? {} : { json: options.body }),
  });
}

export function downloadFeature(path: string, filename: string, scope?: string): Promise<void> {
  return downloadFile(featureUrl(path, { scope }), filename);
}
