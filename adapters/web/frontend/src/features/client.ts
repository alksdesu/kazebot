import { downloadFile } from '../api/blobs';
import { request, requestJson, type RequestOptions } from '../api/request';
import { receiptOf } from '../api/shape';
import { MOUNT } from '../instances/paths';

interface FeatureCall {
  method?: string;
  body?: unknown;
  params?: Record<string, string | number | boolean | null | undefined>;
  scope?: string;
  signal?: AbortSignal;
}

/**
 * 读的给 what：正文不是 JSON 时报「what格式不对：返回的不是 JSON」。
 * 写的给 receipt，先说清已经做完：正文不是 JSON 时抛 ReceiptUnreadable，调用方照做完收尾。
 * 形状由页面按同一句话去认，哪些字段要用它最清楚。
 */
export type FeatureRequestOptions = FeatureCall & ({ what: string; receipt?: undefined } | { receipt: string; what?: undefined });

export function featureUrl(path: string, options: Pick<FeatureCall, 'params' | 'scope'> = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(options.params || {})) {
    if (value !== null && value !== undefined && (value !== '' || key === 'bot_scope')) query.set(key, String(value));
  }
  if (options.scope) query.set('scope', options.scope);
  const suffix = query.toString();
  return `${MOUNT}${path}${suffix ? `?${suffix}` : ''}`;
}

export async function featureRequest(path: string, options: FeatureRequestOptions): Promise<unknown> {
  const url = featureUrl(path, options);
  const init: RequestOptions = {
    method: options.method || 'GET',
    signal: options.signal,
    ...(options.body === undefined ? {} : { json: options.body }),
  };
  if (options.receipt !== undefined) return receiptOf(options.receipt, await request(url, init), (body) => body);
  return requestJson(url, options.what, (body) => body, init);
}

export function downloadFeature(path: string, filename: string, scope?: string): Promise<void> {
  return downloadFile(featureUrl(path, { scope }), filename);
}
