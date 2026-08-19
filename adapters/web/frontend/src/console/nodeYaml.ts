// 节点 yaml 的定点编辑。只动目标那几行，其余原文逐字保留。
//
// 不用 js-yaml round-trip：qq.orchestrator.yaml 的 prompt 是两百行 block scalar，
// dump 一遍会变成带引号的长字符串，注释也全没了。认不出格式就抛，宁可让人去改原文，
// 也不能赌一把把文件写坏。

export class NodeYamlShapeError extends Error {}

const indentOf = (line: string): number => line.length - line.trimStart().length;

/** 拆行时吃掉 \r，写回时还原原来的行尾。仓库里的节点文件是 CRLF，
 *  按 '\n' 硬拆会让每行尾巴留一个 \r（键名就此匹配不上），拼回去又会混入 LF。 */
function splitLines(raw: string): { lines: string[]; eol: string } {
  return { lines: raw.split(/\r?\n/), eol: raw.includes('\r\n') ? '\r\n' : '\n' };
}

/** 找到 key 所在行。path 逐层下探，子键必须比父键缩进更深。 */
function locate(lines: string[], path: string[]): { at: number; indent: number } {
  let from = 0;
  let until = lines.length;
  let parentIndent = -1;
  let found = { at: -1, indent: 0 };

  for (const key of path) {
    const head = `${key}:`;
    let hit = -1;
    for (let i = from; i < until; i += 1) {
      const bare = lines[i].trimStart();
      if (!bare || bare.startsWith('#')) continue;
      const indent = indentOf(lines[i]);
      // 缩进退回父级或更浅，说明父块已经结束，后面同名的键是别人的。
      if (indent <= parentIndent) { until = i; break; }
      if (bare === head || bare.startsWith(`${head} `)) { hit = i; break; }
    }
    if (hit < 0) throw new NodeYamlShapeError(`yaml 里找不到 ${path.join('.')}`);
    parentIndent = indentOf(lines[hit]);
    found = { at: hit, indent: parentIndent };
    from = hit + 1;
  }
  return found;
}

/** key 所辖的块到哪一行为止（不含）。 */
function blockEnd(lines: string[], at: number, indent: number): number {
  let end = at + 1;
  while (end < lines.length) {
    const line = lines[end];
    if (!line.trim()) { end += 1; continue; }
    if (indentOf(line) <= indent) break;
    end += 1;
  }
  // 尾随空行归还给下一块，否则每存一次就多吞一行。
  while (end > at + 1 && !lines[end - 1].trim()) end -= 1;
  return end;
}

/** 替换一个块状列表（`key:` 之后若干 `- item` 行）的全部条目。 */
export function replaceYamlList(raw: string, path: string[], items: readonly string[]): string {
  const { lines, eol } = splitLines(raw);
  const { at, indent } = locate(lines, path);
  const head = lines[at];
  const inline = head.slice(head.indexOf(':') + 1).trim();
  if (inline && !inline.startsWith('#')) {
    // `allow: [a, b]` 这种 flow 写法不在支持范围内，改它要重排整行。
    throw new NodeYamlShapeError(`${path.join('.')} 不是分行列表，请直接编辑原文`);
  }
  const end = blockEnd(lines, at, indent);
  for (let i = at + 1; i < end; i += 1) {
    const bare = lines[i].trimStart();
    if (bare && !bare.startsWith('- ') && !bare.startsWith('#')) {
      throw new NodeYamlShapeError(`${path.join('.')} 下面混着别的内容，请直接编辑原文`);
    }
  }
  const pad = ' '.repeat(indent + 2);
  const body = items.map((item) => `${pad}- ${item}`);
  return [...lines.slice(0, at + 1), ...body, ...lines.slice(end)].join(eol);
}

/** 读一个块状列表的条目。 */
export function readYamlList(raw: string, path: string[]): string[] {
  const { lines } = splitLines(raw);
  let found;
  try {
    found = locate(lines, path);
  } catch {
    return [];
  }
  const end = blockEnd(lines, found.at, found.indent);
  const out: string[] = [];
  for (let i = found.at + 1; i < end; i += 1) {
    const bare = lines[i].trimStart();
    if (bare.startsWith('- ')) out.push(bare.slice(2).trim());
  }
  return out;
}

/** 替换一个标量值。空串表示删掉这一行（回落节点默认）。 */
export function replaceYamlScalar(raw: string, path: string[], value: string): string {
  const { lines, eol } = splitLines(raw);
  let at: number;
  let indent: number;
  try {
    ({ at, indent } = locate(lines, path));
  } catch (error) {
    // 键本来就不在，而要写的又是空值 —— 目标状态已经达成。
    if (!value) return raw;
    throw error;
  }
  if (blockEnd(lines, at, indent) > at + 1) {
    throw new NodeYamlShapeError(`${path.join('.')} 不是单行取值，请直接编辑原文`);
  }
  if (!value) return [...lines.slice(0, at), ...lines.slice(at + 1)].join(eol);
  const key = lines[at].trimStart().split(':')[0];
  return [
    ...lines.slice(0, at),
    `${' '.repeat(indent)}${key}: ${value}`,
    ...lines.slice(at + 1),
  ].join(eol);
}

/** 写一个顶层标量，键不在就插到 afterKey 后面。空值表示删掉这一行。 */
export function upsertYamlScalar(
  raw: string,
  key: string,
  value: string,
  afterKey: string,
): string {
  const { lines, eol } = splitLines(raw);
  const exists = lines.some((line) => line.trimStart().startsWith(`${key}:`) && indentOf(line) === 0);
  if (exists) return replaceYamlScalar(raw, [key], value);
  if (!value) return raw;
  const anchor = lines.findIndex(
    (line) => indentOf(line) === 0 && line.trimStart().startsWith(`${afterKey}:`),
  );
  if (anchor < 0) throw new NodeYamlShapeError(`yaml 里找不到 ${afterKey}，无处插入 ${key}`);
  return [...lines.slice(0, anchor + 1), `${key}: ${value}`, ...lines.slice(anchor + 1)].join(eol);
}

/** 写一个嵌套标量，中间层不存在就顺路建出来。空值表示删掉这一行。 */
export function upsertYamlNested(raw: string, path: string[], value: string): string {
  if (!path.length) throw new NodeYamlShapeError('路径为空');
  const { lines, eol } = splitLines(raw);

  // 从最深处往回退，找出已经存在的那段前缀。
  let known = path.length;
  while (known > 0) {
    try {
      locate(lines, path.slice(0, known));
      break;
    } catch {
      known -= 1;
    }
  }
  if (known === path.length) return replaceYamlScalar(raw, path, value);
  if (!value) return raw;

  const missing = path.slice(known);
  if (!known) {
    const block = missing.map((key, depth) => `${'  '.repeat(depth)}${key}:`);
    block[block.length - 1] = `${'  '.repeat(missing.length - 1)}${missing[missing.length - 1]}: ${value}`;
    const tail = lines.length && !lines[lines.length - 1].trim() ? lines.slice(0, -1) : lines;
    return [...tail, ...block, ''].join(eol);
  }

  const parent = locate(lines, path.slice(0, known));
  const head = lines[parent.at];
  const inline = head.slice(head.indexOf(':') + 1).trim();
  const next = [...lines];
  if (inline) {
    // `options: {}` 这种占位后面不能直接跟缩进子项，那是坏 yaml。空容器可以安全
    // 抹掉换成块写法；真有值就不敢猜，交回给人。
    if (inline !== '{}' && inline !== '[]' && inline !== 'null' && inline !== '~') {
      throw new NodeYamlShapeError(
        `${path.slice(0, known).join('.')} 已经有值 ${inline}，无法在它下面加 ${missing.join('.')}`,
      );
    }
    next[parent.at] = `${' '.repeat(parent.indent)}${head.trimStart().split(':')[0]}:`;
  }

  const insertAt = blockEnd(next, parent.at, parent.indent);
  const block = missing.map((key, depth) => {
    const indent = ' '.repeat(parent.indent + 2 * (depth + 1));
    return depth === missing.length - 1 ? `${indent}${key}: ${value}` : `${indent}${key}:`;
  });
  return [...next.slice(0, insertAt), ...block, ...next.slice(insertAt)].join(eol);
}

export function readYamlScalar(raw: string, path: string[]): string {
  const { lines } = splitLines(raw);
  try {
    const { at } = locate(lines, path);
    const line = lines[at];
    const after = line.slice(line.indexOf(':') + 1).trim();
    return after.replace(/^["']|["']$/g, '');
  } catch {
    return '';
  }
}
