// 群号与 QQ 号的本地备注。只存在这台浏览器里，不进 qq.yaml、不下发模型 ——
// 群名往往带真实身份，写进配置就等于让它进 bot 的上下文。
const STORE_KEY = 'qq_console_aliases';

export type AliasMap = Record<string, string>;

export function readAliases(): AliasMap {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(([, name]) => typeof name === 'string'),
    ) as AliasMap;
  } catch {
    // 存坏了就当没有备注，不该把整页拖垮。
    return {};
  }
}

/** 写一条备注。空串表示删掉，别留一堆空值在 localStorage 里。 */
export function writeAlias(id: number | string, name: string): AliasMap {
  const next = readAliases();
  const key = String(id);
  const token = name.trim();
  if (token) next[key] = token;
  else delete next[key];
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(next));
  } catch {
    // 隐私模式下 localStorage 会抛，备注丢了不影响配置本身。
  }
  return next;
}
