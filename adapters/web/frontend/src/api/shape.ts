// 响应形状：后端一定会发的东西对不上形状就抛「…格式不对」。补成空值的话，界面会把「没读懂」说成「一个都没有」。

export const isJsonObject = (value: unknown): value is Record<string, unknown> => (
  typeof value === 'object' && value !== null && !Array.isArray(value)
);

/** 一种值该是什么样。mismatch 是对不上时接在位置后面的那半句，比如「不是字符串」。 */
export interface Kind<T> {
  readonly ok: (value: unknown) => value is T;
  readonly mismatch: string;
}

export const TEXT: Kind<string> = {
  ok: (value): value is string => typeof value === 'string',
  mismatch: '不是字符串',
};
export const NON_EMPTY_TEXT: Kind<string> = {
  ok: (value): value is string => typeof value === 'string' && value !== '',
  mismatch: '不是非空字符串',
};
export const BOOLEAN: Kind<boolean> = {
  ok: (value): value is boolean => typeof value === 'boolean',
  mismatch: '不是 true 或 false',
};
export const NUMBER: Kind<number> = {
  ok: (value): value is number => typeof value === 'number' && Number.isFinite(value),
  mismatch: '不是数字',
};
export const OBJECT: Kind<Record<string, unknown>> = {
  ok: isJsonObject,
  mismatch: '不是 JSON 对象',
};
export const TEXT_LIST: Kind<string[]> = {
  ok: (value): value is string[] => Array.isArray(value) && value.every(TEXT.ok),
  mismatch: '不是字符串数组',
};

/** 什么值都行，只要在：null 也算。用于后端原样转来的值，比如配置项的内置默认值。 */
export const PRESENT: Kind<unknown> = {
  ok: (value): value is unknown => value !== undefined,
  mismatch: '没有值',
};

/** 接口类型里带问号的字段：缺了照旧，在就得对得上。 */
export const optional = <T>(kind: Kind<T>): Kind<T | undefined> => ({
  ok: (value): value is T | undefined => value === undefined || kind.ok(value),
  mismatch: kind.mismatch,
});

export const nullable = <T>(kind: Kind<T>): Kind<T | null> => ({
  ok: (value): value is T | null => value === null || kind.ok(value),
  mismatch: `${kind.mismatch}，也不是 null`,
});

/** 后端只发这几个值里的一个：认不出的不按哪一个猜。 */
export const oneOf = <T extends string>(...values: readonly T[]): Kind<T> => ({
  ok: (value): value is T => typeof value === 'string' && (values as readonly string[]).includes(value),
  mismatch: `不是 ${values.map((value) => value || '空串').join('、')} 之一`,
});

/** 每一项都是同一种值的数组。项是有字段的对象时用 shapedRows，出错能指到哪一项的哪个字段。 */
export const listOf = <T>(kind: Kind<T>): Kind<T[]> => ({
  ok: (value): value is T[] => Array.isArray(value) && value.every(kind.ok),
  mismatch: `不是数组，或有一项${kind.mismatch}`,
});

/** 每个值都是同一种值的映射，比如按状态数出来的任务数。 */
export const recordOf = <T>(kind: Kind<T>): Kind<Record<string, T>> => ({
  ok: (value): value is Record<string, T> => isJsonObject(value) && Object.values(value).every(kind.ok),
  mismatch: `不是 JSON 对象，或有一项${kind.mismatch}`,
});

export type Shape = Readonly<Record<string, Kind<unknown>>>;
export type Shaped<S extends Shape> = {
  -readonly [K in keyof S]: S[K] extends Kind<infer T> ? T : never;
} & Record<string, unknown>;

// path 是值在响应里的位置，比如 instances[2].current；空串指整个响应。
export const at = (path: string, key: string | number): string => (
  typeof key === 'number' ? `${path}[${key}]` : path ? `${path}.${key}` : key
);

export function formatError(what: string, value: unknown, path: string, mismatch: string): Error {
  if (value === undefined && path) return new Error(`${what}格式不对：缺 ${path}`);
  return new Error(`${what}格式不对：${path ? `${path} ` : '返回的'}${mismatch}`);
}

/** value 得是对象，shape 里的字段都对得上；对不上抛 what 的格式错误。shape 以外的字段原样留着。 */
export function shaped<S extends Shape>(what: string, value: unknown, shape: S, path = ''): Shaped<S> {
  if (!isJsonObject(value)) throw formatError(what, value, path, OBJECT.mismatch);
  for (const [key, kind] of Object.entries(shape)) {
    if (!kind.ok(value[key])) throw formatError(what, value[key], at(path, key), kind.mismatch);
  }
  return value as Shaped<S>;
}

/** value 得是数组，每一项都按 shape 对得上。 */
export function shapedRows<S extends Shape>(what: string, value: unknown, shape: S, path = ''): Shaped<S>[] {
  if (!Array.isArray(value)) throw formatError(what, value, path, '不是数组');
  return value.map((row: unknown, index) => shaped(what, row, shape, at(path, index)));
}

/** value 得是对象，每个键底下的值都按 shape 对得上。 */
export function shapedRecord<S extends Shape>(what: string, value: unknown, shape: S, path = ''): Record<string, Shaped<S>> {
  if (!isJsonObject(value)) throw formatError(what, value, path, OBJECT.mismatch);
  return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, shaped(what, entry, shape, at(path, key))]));
}

/** 整个值按 kind 核。用于顶层不是对象的响应，比如工具名清单。 */
export function shapedValue<T>(what: string, value: unknown, kind: Kind<T>, path = ''): T {
  if (!kind.ok(value)) throw formatError(what, value, path, kind.mismatch);
  return value;
}

/** 读 2xx 的正文。不是 JSON 就报 what 格式不对：SyntaxError 的原话是英文，也说不出是哪一份没读懂。 */
export async function jsonOf(what: string, resp: Response): Promise<unknown> {
  try {
    return await resp.json();
  } catch (error) {
    // 读到一半被中止、断网这些照原样抛，调用方按作废或网络故障处理。
    if (error instanceof SyntaxError) throw new Error(`${what}格式不对：返回的不是 JSON`);
    throw error;
  }
}

/** 改动在后端已经做完，只是回执读不懂。调用方照做完收尾、另读一遍现状，再单独交代回执的事：当成失败，人会再点一次。 */
export class ReceiptUnreadable extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ReceiptUnreadable';
  }
}

/** 按 read 读 2xx 的回执。正文不是 JSON、形状对不上都抛 ReceiptUnreadable；what 打头的那半句得先说清已经做完。 */
export async function receiptOf<T>(what: string, resp: Response, read: (body: unknown) => T): Promise<T> {
  let body: unknown;
  try {
    body = await resp.json();
  } catch {
    throw new ReceiptUnreadable(`${what}不是 JSON`);
  }
  return readReceipt(what, body, read);
}

/** 已经到手的回执正文按 read 认。认不出抛 ReceiptUnreadable：写照样算做完了。 */
export function readReceipt<T>(what: string, body: unknown, read: (body: unknown) => T): T {
  try {
    return read(body);
  } catch (error) {
    const message = error instanceof Error ? error.message : '';
    // 有的读法报错不带 what：接在 what 后面，人才知道这件事其实已经做完了。
    throw new ReceiptUnreadable(!message ? `${what}读不懂` : message.startsWith(what) ? message : `${what}读不懂：${message}`);
  }
}

/** 写操作的结果。存上了、只是回执读不懂时 result 是 null，receiptProblem 写着哪里没读懂。 */
export interface Receipted<R> {
  result: R | null;
  receiptProblem: string;
}

/** 等写操作做完。回执读不懂不算失败，交回 null 和原因，由调用方照存上了收尾；别的错误照抛。 */
export async function receipted<R>(write: Promise<R>): Promise<Receipted<R>> {
  try {
    return { result: await write, receiptProblem: '' };
  } catch (error) {
    if (error instanceof ReceiptUnreadable) return { result: null, receiptProblem: error.message };
    throw error;
  }
}
