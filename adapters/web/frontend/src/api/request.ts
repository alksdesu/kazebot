// 控制台发往本实例 API 的请求都从这里出：带不带令牌、等多久、失败怎么报，只在这一处定。
import { useSettingsStore } from '../store/settingsStore';
import { fetchWithDeadline } from './requestDeadline';
import { jsonOf } from './shape';

const ERROR_BODY_LIMIT = 4000;
const TRANSIENT_STATUSES: ReadonlySet<number> = new Set([502, 503, 504, 530]);

/** 服务器回了非 2xx。message 以状态码打头便于直接展示，判错一律看 status。 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`${status}${detail ? ` ${detail}` : ''}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export const hasStatus = (error: unknown, ...statuses: number[]): boolean => (
  error instanceof ApiError && statuses.includes(error.status)
);

/** 要的东西不存在。 */
export const isNotFound = (error: unknown): boolean => hasStatus(error, 404);

/** 带着指纹写回时 409：打开之后这份配置被别处改过。别的写入的 409 另有含义（比如系统节点不让改），不能拿它判断。 */
export const isEditConflict = (error: unknown): boolean => hasStatus(error, 409);

export const isUnauthorized = (error: unknown): boolean => hasStatus(error, 401, 403);

/** 网络断了、网关或隧道没接上：等一等再试多半就好。服务器明确回绝的不算。 */
export function isTransientApiError(error: unknown): boolean {
  return !(error instanceof ApiError) || TRANSIENT_STATUSES.has(error.status);
}

/** 给人看的一句失败原因。 */
export function errorMessage(error: unknown, fallback = '请求失败，请重试'): string {
  if (error instanceof Error) return error.message || fallback;
  return typeof error === 'string' && error ? error : fallback;
}

const htmlErrorText = (body: string): string => body
  .replace(/<script[\s\S]*?<\/script>/gi, ' ')
  .replace(/<style[\s\S]*?<\/style>/gi, ' ')
  .replace(/<[^>]*>/g, ' ')
  .replace(/&nbsp;/gi, ' ')
  .replace(/&amp;/gi, '&')
  .replace(/&lt;/gi, '<')
  .replace(/&gt;/gi, '>')
  .replace(/&quot;/gi, '"')
  .replace(/&#39;/gi, "'")
  .replace(/\s+/g, ' ')
  .trim();

export async function errorDetail(response: Response): Promise<string> {
  const body = await response.text();
  if (!body.trim()) return response.statusText;
  try {
    const payload = JSON.parse(body);
    const detail = payload?.detail ?? payload?.message ?? payload?.error;
    if (typeof detail === 'string' && detail.trim()) return detail.trim();
    if (detail && typeof detail === 'object') return JSON.stringify(detail);
  } catch {
    // 代理和隧道的报错页不是 JSON。
  }
  return (response.headers.get('content-type') || '').includes('text/html')
    ? htmlErrorText(body).slice(0, ERROR_BODY_LIMIT)
    : body.trim().slice(0, ERROR_BODY_LIMIT);
}

export interface RequestOptions extends Omit<RequestInit, 'headers' | 'body'> {
  headers?: Record<string, string>;
  body?: BodyInit | null;
  /** 按 JSON 发的请求体，Content-Type 一并带上。 */
  json?: unknown;
  /** 发起操作时记下的那个令牌；不给就用当前登录的。 */
  token?: string | null;
  /** 公开端点，不带令牌。 */
  anonymous?: boolean;
  /** 等响应头的上限；不给按方法取默认，null 表示不限。 */
  timeoutMs?: number | null;
  /** 不经插件的请求包装层：插件管理、登录校验这类请求不能被坏插件卡死。 */
  bypassPlugins?: boolean;
}

export const currentAdminToken = (): string => useSettingsStore.getState().adminToken || '';

const TOKEN_REFUSED = '服务端不认当前令牌（令牌无效或已过期），请重新登录。';

// 登录页验令牌时还没登录，不算：不然验一次错的令牌就被拉回登录页，没完没了
function expireRefusedLogin(credential: string): void {
  const settings = useSettingsStore.getState();
  if (settings.isAuthenticated && credential && credential === settings.adminToken) settings.expireLogin(TOKEN_REFUSED);
}

/** 拼好请求发出去，不看状态码，插件 SDK 要原样拿到 Response。带着当前登录的令牌被回 401 时，和事件流 4401 一样回登录页。 */
export async function send(url: string, options: RequestOptions = {}): Promise<Response> {
  const { headers: extra, json, body, token, anonymous, timeoutMs, bypassPlugins, ...rest } = options;
  const headers: Record<string, string> = { ...extra };
  if (json !== undefined) headers['Content-Type'] = 'application/json';
  const init: RequestInit = { ...rest };
  if (Object.keys(headers).length) init.headers = headers;
  if (json !== undefined) init.body = JSON.stringify(json);
  else if (body !== undefined) init.body = body;
  const credential = anonymous ? '' : (token || currentAdminToken());
  const response = await fetchWithDeadline(url, init, timeoutMs, { bypassPlugins, credential });
  if (response.status === 401) expireRefusedLogin(credential);
  return response;
}

/** 发请求，非 2xx 抛 ApiError。 */
export async function request(url: string, options: RequestOptions = {}): Promise<Response> {
  const response = await send(url, options);
  if (!response.ok) throw new ApiError(response.status, await errorDetail(response));
  return response;
}

/** 发请求读 2xx 的 JSON 正文，按 read 认形状。正文不是 JSON 时报「what格式不对：返回的不是 JSON」。 */
export async function requestJson<T>(
  url: string, what: string, read: (body: unknown) => T, options: RequestOptions = {},
): Promise<T> {
  return read(await jsonOf(what, await request(url, options)));
}
