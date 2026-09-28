// yaml 原文的逐行定位：按缩进找键、量出一个键管到哪一行。只认块写法，认不出就抛，调用方据此不去动原文。

export class YamlShapeError extends Error {}

export interface KeyLine {
  /** 键所在的行号。 */
  at: number;
  indent: number;
  /** 键在这一行里的原文，带引号就含引号，不含冒号。 */
  keyText: string;
}

/** 可以直接抹掉、换成块写法的空取值。 */
export const EMPTY_PLACEHOLDERS: ReadonlySet<string> = new Set(['{}', '[]', 'null', '~']);

export const indentOf = (line: string): number => line.length - line.trimStart().length;

/** 既不是空行也不是注释。 */
export const isContent = (line: string): boolean => {
  const bare = line.trimStart();
  return Boolean(bare) && !bare.startsWith('#');
};

export const isSequenceItem = (line: string): boolean => {
  const bare = line.trimStart();
  return bare === '-' || bare.startsWith('- ');
};

/** 行内取值是块标量的开头：`|`、`>-`、`|2+` 这类。 */
export const isBlockScalarHead = (value: string): boolean => /^[|>][1-9+-]{0,2}$/.test(value);

/** 拆行时吃掉 \r，写回时还原原来的行尾。仓库里的节点文件是 CRLF，
 *  按 '\n' 硬拆会让每行尾巴留一个 \r（键名就此匹配不上），拼回去又会混入 LF。 */
export function splitLines(raw: string): { lines: string[]; eol: string } {
  return { lines: raw.split(/\r?\n/), eol: raw.includes('\r\n') ? '\r\n' : '\n' };
}

/** 第一行内容的缩进，也就是这一段最外层键的缩进。一行内容都没有时是 0。 */
export function topIndent(lines: readonly string[]): number {
  const first = lines.find(isContent);
  return first === undefined ? 0 : indentOf(first);
}

/** 键冒号后面那一段里的行尾注释（含前导空白），没有是空串。按 YAML 的规矩扫：引号里的 # 不算，# 前面得有空白。
 *  普通标量里夹着引号（it's）时认不准引号范围，宁可不认 —— 顶多丢一句注释，不会把取值错切成注释。 */
export function commentAfter(rest: string): string {
  let quote = '';
  for (let i = 0; i < rest.length; i += 1) {
    const ch = rest[i];
    if (quote === '"') {
      if (ch === '\\') i += 1;
      else if (ch === '"') quote = '';
    } else if (quote === "'") {
      if (ch === "'" && rest[i + 1] === "'") i += 1;
      else if (ch === "'") quote = '';
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === '#' && (i === 0 || /\s/.test(rest[i - 1]))) {
      let start = i;
      while (start > 0 && /\s/.test(rest[start - 1])) start -= 1;
      return rest.slice(start);
    }
  }
  return '';
}

/** 一个键在原文里可能的写法：裸写，或者带双引号、单引号。文件里的 `"2024": x` 也得认得出来。 */
const keySpellings = (key: string): string[] => [key, JSON.stringify(key), `'${key.replace(/'/g, "''")}'`];

/** 去掉缩进的这一行是不是 key 的映射项，是的话返回键的原文。 */
function keyAt(bare: string, key: string): string | null {
  for (const spelling of keySpellings(key)) {
    const head = `${spelling}:`;
    if (bare === head || (bare.startsWith(head) && /\s/.test(bare[head.length]))) return spelling;
  }
  return null;
}

/** 键那一行的行内取值（不含行尾注释）和行尾注释（含前导空白）。块写法的键取值是空串。
 *  按键的原文长度切，不按第一个冒号切：`a:b: 1`、`"x:y": 1` 这种键名里带冒号的也切得对。 */
export function keyParts(lines: readonly string[], key: KeyLine): { value: string; comment: string } {
  const rest = lines[key.at].slice(key.indent + key.keyText.length + 1);
  const comment = commentAfter(rest);
  return { value: rest.slice(0, rest.length - comment.length).trim(), comment };
}

/** 缩进加键的原文，改写这一行时原样留着。 */
export const keyHead = (lines: readonly string[], key: KeyLine): string => lines[key.at].slice(0, key.indent + key.keyText.length);

/** 找 key 所在行，找不到返回 null。path 逐层下探，每一层只认直属子键。 */
export function findKey(lines: readonly string[], path: readonly string[]): KeyLine | null {
  let from = 0;
  let parentIndent = -1;
  let found: KeyLine | null = null;

  for (const key of path) {
    let childIndent = -1;
    let hit: KeyLine | null = null;
    for (let i = from; i < lines.length && !hit; i += 1) {
      if (!isContent(lines[i])) continue;
      const indent = indentOf(lines[i]);
      // 缩进退回父级或更浅，说明父块已经结束，后面同名的键是别人的。
      if (indent <= parentIndent) break;
      // 更深处的同名键是孙辈的；块标量正文里长得像键的行也都在更深处。
      if (childIndent < 0) childIndent = indent;
      const keyText = indent === childIndent ? keyAt(lines[i].trimStart(), key) : null;
      if (keyText !== null) hit = { at: i, indent, keyText };
    }
    if (!hit) return null;
    parentIndent = hit.indent;
    found = hit;
    from = hit.at + 1;
  }
  return found;
}

/** 同 findKey，找不到就抛。 */
export function locate(lines: readonly string[], path: readonly string[]): KeyLine {
  const found = findKey(lines, path);
  if (!found) throw new YamlShapeError(`yaml 里找不到 ${path.join('.')}`);
  return found;
}

/** 键所辖的块到哪一行为止（不含）。块尾的空行和注释归还给下一块，否则每存一次就多吞几行。 */
export function blockEnd(lines: readonly string[], key: KeyLine): number {
  const { value } = keyParts(lines, key);
  // 块标量正文里 # 开头的行也是正文，只能按缩进量。
  const scalar = isBlockScalarHead(value);
  // 键后面没写取值时，和它同一缩进的「- 」行仍是它的列表项：YAML 允许列表不缩进，PyYAML 默认就这么写。
  const ownsDashes = !value;
  let end = key.at + 1;
  for (let i = key.at + 1; i < lines.length; i += 1) {
    const line = lines[i];
    if (!line.trim() || (!scalar && !isContent(line))) continue;
    const lineIndent = indentOf(line);
    if (lineIndent < key.indent || (lineIndent === key.indent && !(ownsDashes && isSequenceItem(line)))) break;
    end = i + 1;
  }
  return end;
}

/** [from, to) 里最后一行内容之后。尾随的空行和注释不算，留给后面那一块。 */
export function contentEnd(lines: readonly string[], from: number, to: number): number {
  let end = to;
  while (end > from && !isContent(lines[end - 1])) end -= 1;
  return end;
}

/** 要往 parent 底下加子键：`extensions: {}` 这种空占位换成块写法，行尾注释留着。
 *  真有值（标量、流式映射）不敢猜，抛出去交回给人。 */
export function openBlock(
  lines: string[],
  parent: KeyLine,
  parentPath: readonly string[],
  childPath: readonly string[],
): void {
  const { value, comment } = keyParts(lines, parent);
  if (!value) return;
  if (!EMPTY_PLACEHOLDERS.has(value)) {
    throw new YamlShapeError(`${parentPath.join('.')} 已经有值 ${value}，无法在它下面加 ${childPath.join('.')}`);
  }
  lines[parent.at] = `${keyHead(lines, parent)}:${comment}`;
}
