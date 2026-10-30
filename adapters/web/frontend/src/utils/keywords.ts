// 关键词输入框的拆法，和 engine/builtin/knowledge_inject.py 的 compile_keyword 认正则用同一个判据。

// 整段形如 /pattern/flags 才算正则，flags 只认 i、s、m。
const REGEX_KEYWORD = /^\/[\s\S]+\/[ism]*$/;

const opensRegex = (part: string): boolean => part.trimStart().startsWith('/');

/** 从 start 那段起凑成整段正则时最后一段的下标；凑不成就是 start 自己。 */
function regexEnd(parts: readonly string[], start: number): number {
  if (!opensRegex(parts[start])) return start;
  for (let end = start; end < parts.length; end += 1) {
    // 以 / 开头的一段是下一个词：「/help, /sim」是两条命令，接起来却正好像一段带 flags 的正则。
    if (end > start && opensRegex(parts[end])) break;
    if (REGEX_KEYWORD.test(parts.slice(start, end + 1).join(',').trim())) return end;
  }
  return start;
}

/** 按逗号拆关键词。写成 /正则/ 的那段整段算一个，里面的逗号不拆：拆开的两截都不成正则，存下的和召回时认的就对不上了。 */
export function splitKeywords(text: string): string[] {
  const parts = text.split(',');
  const words: string[] = [];
  let start = 0;
  while (start < parts.length) {
    const end = regexEnd(parts, start);
    words.push(parts.slice(start, end + 1).join(',').trim());
    start = end + 1;
  }
  return words.filter(Boolean);
}

/** 关键词输入框的写法说明，记忆和技能两处共用。 */
export const KEYWORD_INPUT_HINT = '英文逗号分隔；写成 /正则/ 的整段算一个，里面的逗号不拆，末尾可加 i、s、m';
