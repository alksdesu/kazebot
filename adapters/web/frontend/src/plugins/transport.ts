import { MOUNT } from '../instances/paths';
import { extensions } from './registry';

export type RequestHandler = (input: string, init: RequestInit) => Promise<Response>;

export interface TransportOptions {
  /** 不经插件的请求包装层。 */
  bypassPlugins?: boolean;
  /** 发往本实例 API 时补上的管理员令牌。 */
  credential?: string;
}

extensions.builtin<RequestHandler>('transport', 'request', (input, init) => fetch(input, init));

const isInstanceApi = (url: URL): boolean => (
  url.origin === window.location.origin && url.pathname.startsWith(`${MOUNT}/v1/`)
);

function withCredential(init: RequestInit, credential: string): RequestInit {
  const authorization = `Bearer ${credential}`;
  if (init.headers instanceof Headers || Array.isArray(init.headers)) {
    const headers = new Headers(init.headers);
    headers.set('Authorization', authorization);
    return { ...init, headers };
  }
  return { ...init, headers: { ...init.headers, Authorization: authorization } };
}

export function dispatchRequest(input: string, init: RequestInit, options: TransportOptions = {}): Promise<Response> {
  // 令牌到最内层才补：插件的包装层看不到它，包装层把请求改发到别处时也不会带过去。
  const base: RequestHandler = (url, request) => {
    const credential = options.credential;
    return fetch(url, credential && isInstanceApi(new URL(url, window.location.href)) ? withCredential(request, credential) : request);
  };
  const path = new URL(input, window.location.href).pathname;
  const recovery = /\/v1\/(?:admin\/(?:plugins|auth|runtime|config\/runtime)(?:\/|$)|(?:auth|login|logout|health)(?:\/|$))/.test(path);
  return (options.bypassPlugins || recovery ? base : extensions.resolve('transport', 'request', base)!)(input, init);
}
