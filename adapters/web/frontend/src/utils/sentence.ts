// 提示文字的拼接：几句话接成一段时补齐句号、去掉重复。

const sentencesOf = (text: string): string[] => text.split(/[。！？]/).map((part) => part.trim()).filter(Boolean);

/** 在已有的一段提示后面再接一句。前一句没收尾就补个句号，免得两句连成一句；任一边是空的就只剩另一边。
 *  要接的话已经一句不落地说过了就不再接：后台重读反复失败时报的是同样几个原因，一直往后接提示会越来越长。 */
export function appendSentence(current: string, next: string): string {
  if (!current || !next) return current || next;
  const said = new Set(sentencesOf(current));
  if (sentencesOf(next).every((sentence) => said.has(sentence))) return current;
  return `${/[。！？]$/.test(current) ? current : `${current}。`}${next}`;
}

/** 保存成功的那句话，后面接上注释没保住的交代和后端校验给的提醒。 */
export function savedMessage(done: string, note = '', warnings: readonly string[] = []): string {
  return [note && `${note}。`, ...warnings].reduce(appendSentence, done);
}

/** 存上了、回执没读懂，手上留着的还是存之前那一版指纹时交代的那句。reload 是这一页读入最新内容的说法。 */
export function reloadBeforeEditing(receiptProblem: string, reload = '重新载入'): string {
  return appendSentence(receiptProblem, `${reload}再改，不然再存会被当成冲突拒掉`);
}
