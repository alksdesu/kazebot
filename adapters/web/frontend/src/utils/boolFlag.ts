// 与 clonoth_runtime.parse_bool 认同一套写法：手写的 yes、on、1 算开，no、off、0 算关，认不出的照 fallback，和引擎读出同一个结论
const PARSE_BOOL_TRUE_WORDS = new Set(['1', 'true', 'yes', 'y', 'on']);
const PARSE_BOOL_FALSE_WORDS = new Set(['0', 'false', 'no', 'n', 'off']);

export function parseBoolFlag(value: unknown, fallback = false): boolean {
  if (typeof value === 'boolean') return value;
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim().toLowerCase();
  if (PARSE_BOOL_TRUE_WORDS.has(text)) return true;
  if (PARSE_BOOL_FALSE_WORDS.has(text)) return false;
  return fallback;
}
