// 群号与账号的本地备注。只存在这台浏览器里，不进平台配置、不下发模型 ——
// 群名往往带真实身份，写进配置就等于让它进 bot 的上下文。
import { QQ_NAPCAT } from '../platforms/registry';
import { scopedKey } from '../store/storageKey';

const STORE_KEY = scopedKey('qq_console_aliases');

export type AliasMap = Record<string, string>;

/** 备注的键。裸 ID 会跨平台撞车：Telegram 的群 ID 和 QQ 群号都是一串数字。 */
export function aliasKey(platformId: string, id: number | string): string {
  return `${platformId}:${id}`;
}

/** 改版前按裸 ID 存，那时只有 NapCat QQ 一个平台，原样划到它名下。 */
function migrateBareIds(stored: AliasMap): AliasMap {
  const migrated: AliasMap = {};
  for (const [key, name] of Object.entries(stored)) {
    migrated[key.includes(':') ? key : aliasKey(QQ_NAPCAT.id, key)] = name;
  }
  return migrated;
}

export function readAliases(): AliasMap {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return migrateBareIds(Object.fromEntries(
      Object.entries(parsed).filter(([, name]) => typeof name === 'string'),
    ) as AliasMap);
  } catch {
    // 存坏了就当没有备注，不该把整页拖垮。
    return {};
  }
}

/** 写一条备注。空串表示删掉，别留一堆空值在 localStorage 里。 */
export function writeAlias(platformId: string, id: number | string, name: string): AliasMap {
  const next = readAliases();
  const key = aliasKey(platformId, id);
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
