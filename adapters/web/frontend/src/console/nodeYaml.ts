// 节点 yaml 的定点编辑。只动目标那几行，其余原文逐字保留。
//
// 不用 js-yaml round-trip：qq.orchestrator.yaml 的 prompt 是两百行 block scalar，
// dump 一遍会变成带引号的长字符串，注释也全没了。认不出格式就抛，宁可让人去改原文，
// 也不能赌一把把文件写坏。

import yaml from 'js-yaml';

import {
  YamlShapeError as NodeYamlShapeError,
  blockEnd,
  findKey,
  indentOf,
  keyHead,
  keyParts,
  locate,
  openBlock,
  splitLines,
} from './yamlText';

export { NodeYamlShapeError };

/** 自由输入的文字写成 YAML 字符串标量：原样写读回来还是同一个字符串（按 YAML 1.1 读也一样）就不加引号，否则加双引号。
 *  下面几个写入函数收的是标量原文，数字、布尔要原样写进去；只有当字符串用的值才先过这一道。 */
export function yamlString(text: string): string {
  const dumped = yaml.dump(text, { lineWidth: -1, quotingType: '"' }).trimEnd();
  return dumped.includes('\n') ? JSON.stringify(text) : dumped;
}

/** 行内取值还原成文字。带引号的交给 js-yaml 解析，转义才还原得对；裸写的原样返回，数字照用户写的样子显示。 */
function scalarText(value: string): string {
  if (!/^(["']).*\1$/.test(value)) return value;
  try {
    const parsed = yaml.load(value);
    return typeof parsed === 'string' ? parsed : value;
  } catch {
    return value.slice(1, -1);
  }
}

/** 替换一个块状列表（`key:` 之后若干 `- item` 行）的全部条目。 */
export function replaceYamlList(raw: string, path: string[], items: readonly string[]): string {
  const { lines, eol } = splitLines(raw);
  const key = locate(lines, path);
  if (keyParts(lines, key).value) {
    // `allow: [a, b]` 这种 flow 写法不在支持范围内，改它要重排整行。
    throw new NodeYamlShapeError(`${path.join('.')} 不是分行列表，请直接编辑原文`);
  }
  const end = blockEnd(lines, key);
  for (let i = key.at + 1; i < end; i += 1) {
    const bare = lines[i].trimStart();
    if (bare && !bare.startsWith('- ') && !bare.startsWith('#')) {
      throw new NodeYamlShapeError(`${path.join('.')} 下面混着别的内容，请直接编辑原文`);
    }
  }
  // 跟着原来的条目缩进走：不缩进的列表（PyYAML 的写法）改完还是不缩进。
  const first = lines.slice(key.at + 1, end).find((line) => line.trimStart().startsWith('- '));
  const pad = ' '.repeat(first === undefined ? key.indent + 2 : indentOf(first));
  const body = items.map((item) => `${pad}- ${item}`);
  return [...lines.slice(0, key.at + 1), ...body, ...lines.slice(end)].join(eol);
}

/** 读一个块状列表的条目。 */
export function readYamlList(raw: string, path: string[]): string[] {
  const { lines } = splitLines(raw);
  const found = findKey(lines, path);
  if (!found) return [];
  const end = blockEnd(lines, found);
  const out: string[] = [];
  for (let i = found.at + 1; i < end; i += 1) {
    const bare = lines[i].trimStart();
    if (bare.startsWith('- ')) out.push(bare.slice(2).trim());
  }
  return out;
}

/** 替换一个标量值。空串表示删掉这一行（回落节点默认）。value 是标量原文。 */
export function replaceYamlScalar(raw: string, path: string[], value: string): string {
  const { lines, eol } = splitLines(raw);
  const key = findKey(lines, path);
  if (!key) {
    // 键本来就不在，而要写的又是空值 —— 目标状态已经达成。
    if (!value) return raw;
    throw new NodeYamlShapeError(`yaml 里找不到 ${path.join('.')}`);
  }
  if (blockEnd(lines, key) > key.at + 1) {
    throw new NodeYamlShapeError(`${path.join('.')} 不是单行取值，请直接编辑原文`);
  }
  if (!value) return [...lines.slice(0, key.at), ...lines.slice(key.at + 1)].join(eol);
  return [
    ...lines.slice(0, key.at),
    `${keyHead(lines, key)}: ${value}${keyParts(lines, key).comment}`,
    ...lines.slice(key.at + 1),
  ].join(eol);
}

/** 写一个顶层标量，键不在就插到 afterKey 那一块后面。空值表示删掉这一行。value 是标量原文。 */
export function upsertYamlScalar(
  raw: string,
  key: string,
  value: string,
  afterKey: string,
): string {
  const { lines, eol } = splitLines(raw);
  if (findKey(lines, [key])) return replaceYamlScalar(raw, [key], value);
  if (!value) return raw;
  const anchor = findKey(lines, [afterKey]);
  if (!anchor) throw new NodeYamlShapeError(`yaml 里找不到 ${afterKey}，无处插入 ${key}`);
  // 插在 afterKey 整块之后：它要是跟着多行取值，插在它那一行后面就把块切断了。
  const at = blockEnd(lines, anchor);
  return [...lines.slice(0, at), `${' '.repeat(anchor.indent)}${key}: ${value}`, ...lines.slice(at)].join(eol);
}

/** 从最深处往回退，数出 path 里已经存在的那段前缀有几层。 */
function knownDepth(lines: readonly string[], path: readonly string[]): number {
  let known = path.length;
  while (known > 0 && !findKey(lines, path.slice(0, known))) known -= 1;
  return known;
}

/** 写一个嵌套标量，中间层不存在就顺路建出来。空值表示删掉这一行。value 是标量原文。 */
export function upsertYamlNested(raw: string, path: string[], value: string): string {
  if (!path.length) throw new NodeYamlShapeError('路径为空');
  const { lines, eol } = splitLines(raw);

  const known = knownDepth(lines, path);
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
  const next = [...lines];
  openBlock(next, parent, path.slice(0, known), missing);

  const insertAt = blockEnd(next, parent);
  const block = missing.map((key, depth) => {
    const indent = ' '.repeat(parent.indent + 2 * (depth + 1));
    return depth === missing.length - 1 ? `${indent}${key}: ${value}` : `${indent}${key}:`;
  });
  return [...next.slice(0, insertAt), ...block, ...next.slice(insertAt)].join(eol);
}

/** 写一个块状列表，键不在就顺路建出来。mode:all 的节点通常没有 allow 键。 */
export function upsertYamlList(raw: string, path: string[], items: readonly string[]): string {
  if (!path.length) throw new NodeYamlShapeError('路径为空');
  const { lines, eol } = splitLines(raw);

  const known = knownDepth(lines, path);
  if (known === path.length) return replaceYamlList(raw, path, items);

  const missing = path.slice(known);
  const render = (baseIndent: number): string[] => {
    const head = missing.map((key, depth) => `${' '.repeat(baseIndent + 2 * depth)}${key}:`);
    const pad = ' '.repeat(baseIndent + 2 * missing.length);
    return [...head, ...items.map((item) => `${pad}- ${item}`)];
  };

  if (!known) {
    const tail = lines.length && !lines[lines.length - 1].trim() ? lines.slice(0, -1) : lines;
    return [...tail, ...render(0), ''].join(eol);
  }

  const parent = locate(lines, path.slice(0, known));
  const next = [...lines];
  openBlock(next, parent, path.slice(0, known), missing);
  const insertAt = blockEnd(next, parent);
  return [...next.slice(0, insertAt), ...render(parent.indent + 2), ...next.slice(insertAt)].join(eol);
}

export function readYamlScalar(raw: string, path: string[]): string {
  const { lines } = splitLines(raw);
  const found = findKey(lines, path);
  // 行尾注释不算取值，否则改一次值就把注释又抄一遍写回去。
  return found ? scalarText(keyParts(lines, found).value) : '';
}
