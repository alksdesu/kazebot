// 请求的时限：读请求迟迟拿不到响应头就中止并报超时，页面不会因为一次挂住的读取一直转圈、按钮一直灰着。

/** 读请求等响应头的上限。后端调 NapCat 这类外部服务自己有 20 秒超时，一分钟足够宽松。 */
export const READ_TIMEOUT_MS = 60_000;

export class RequestTimeoutError extends Error {
  constructor(readonly timeoutMs: number) {
    super(`请求超时：${Math.round(timeoutMs / 1000)} 秒内服务器没有应答`);
    this.name = 'RequestTimeoutError';
  }
}

// 写操作不设默认时限：服务器可能还在做，客户端先放弃只会让人以为没做成。
const defaultTimeout = (init: RequestInit): number | null => {
  const method = (init.method || 'GET').toUpperCase();
  return method === 'GET' || method === 'HEAD' ? READ_TIMEOUT_MS : null;
};

/** 发请求，过了时限还没拿到响应头就中止。时限只管到响应头，下载和读正文不受限；调用方传的 signal 一直管到正文读完。 */
export async function fetchWithDeadline(
  input: string,
  init: RequestInit = {},
  timeoutMs: number | null = defaultTimeout(init),
): Promise<Response> {
  if (timeoutMs === null) return fetch(input, init);
  const controller = new AbortController();
  const outer = init.signal;
  const forward = () => controller.abort(outer?.reason);
  if (outer?.aborted) forward();
  else outer?.addEventListener('abort', forward, { once: true });
  const expired = new RequestTimeoutError(timeoutMs);
  const timer = setTimeout(() => controller.abort(expired), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    // 拿到响应头之后转发要留着，调用方才取消得了读到一半的正文；没拿到就没有正文可取消了。
    outer?.removeEventListener('abort', forward);
    // 按先中止的那一方报：调用方先取消的，随后时限到点也不改报成超时。
    if (controller.signal.reason === expired) throw expired;
    throw error;
  } finally {
    clearTimeout(timer);
  }
}
