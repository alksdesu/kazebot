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

/** 写已经做完之后重读现状没成时补的那句。只补这一句：换成一条失败的话，人会以为没做成，再做一次。 */
export function rereadMissed(problem: string): string {
  return `刚才重新读取也没成：${problem}`;
}

/** 存上了、回执没读懂，重读现状也没成时交代的那句：手上这份还是存之前的，得先读入最新内容再改。用于不带指纹的写。 */
export function rereadFailed(receiptProblem: string, problem: string, reload: string): string {
  return [`${reload}再改`, rereadMissed(problem)].reduce(appendSentence, receiptProblem);
}
