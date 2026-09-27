// 一份 schema 的分区可以分摆在几页上。每页认领自己摆出来的那几块，侧栏搜索据此把结果指到真正摆着它的那页。
export type SectionPick =
  | { readonly only: readonly string[] }
  | { readonly except: readonly string[] };

/** 这一块归不归这页。不给 pick 就是整份 schema 都归它。 */
export function picksSection(pick: SectionPick | undefined, id: string): boolean {
  if (!pick) return true;
  return 'only' in pick ? pick.only.includes(id) : !pick.except.includes(id);
}
