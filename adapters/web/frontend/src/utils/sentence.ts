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
