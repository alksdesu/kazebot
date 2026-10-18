import { extensions } from './registry';

export type RequestHandler = (input: string, init: RequestInit) => Promise<Response>;
extensions.builtin<RequestHandler>('transport', 'request', (input, init) => fetch(input, init));

export function dispatchRequest(input: string, init: RequestInit, bypass = false): Promise<Response> {
  const base: RequestHandler = (url, options) => fetch(url, options);
  const path = new URL(input, window.location.href).pathname;
  const recovery = /\/v1\/(?:admin\/(?:plugins|auth|runtime|config\/runtime)(?:\/|$)|(?:auth|login|logout|health)(?:\/|$))/.test(path);
  return (bypass || recovery ? base : extensions.resolve('transport', 'request', base)!)(input, init);
}
