// 把改过的文档写回 yaml 原文：只重写变了的键，注释、空行和其余原文逐字保留。
//
// 改完重新解析一遍和目标逐项比对。对不上（流式映射这类认不出的写法）就整份重写，
// 并如实告诉调用方注释没保住 —— 绝不写出一份解析结果和目标不同的文件。
import yaml from 'js-yaml';

import {
  YamlShapeError,
  blockEnd,
  commentAfter,
  contentEnd,
  findKey,
  indentOf,
  isBlockScalarHead,
  isContent,
  isSequenceItem,
  keyHead,
  keyParts,
  openBlock,
  splitLines,
  topIndent,
  type KeyLine,
} from './yamlText';

export interface YamlRewrite {
  content: string;
  /** 原文里的注释一行没少。false 时 note 说清是整份重写了，还是整块改写的地方夹着注释。 */
  preserved: boolean;
  /** 没保住时交代给人的那句，不带句末标点，由调用方按上下文接；保住了是空串。 */
  note: string;
}

export const WHOLE_FILE_REWRITTEN = '原文里有认不出的写法，这次按整份重写，文件里的注释和空行没能保留';
export const BLOCK_COMMENTS_DROPPED = '改动的那几项是整块重写的，块里原有的注释没能保留';

type Mapping = Record<string, unknown>;

export type YamlEdit =
  | { op: 'set'; path: string[]; value: unknown; before?: unknown }
  | { op: 'delete'; path: string[] };

/** 改完的行，和整块替换时连带丢掉的注释行数。 */
interface Step {
  lines: string[];
  dropped: number;
}

// js-yaml 把时间戳读成 Date，它是叶子值，不能当映射往下钻。
const isMapping = (value: unknown): value is Mapping => (
  Boolean(value) && typeof value === 'object' && !Array.isArray(value) && !(value instanceof Date)
);

const isScalar = (value: unknown): boolean => value === null || typeof value !== 'object' || value instanceof Date;

const nonEmptyMapping = (value: unknown): value is Mapping => isMapping(value) && Object.keys(value).length > 0;

const has = (value: Mapping, key: string): boolean => Object.prototype.hasOwnProperty.call(value, key);

const PLAIN_KEY = /^[A-Za-z_][A-Za-z0-9_-]*$/;

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (isMapping(value)) {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  }
  return value;
}

/** 深比较，映射不看键序：插进来的键排在哪儿不影响解析结果。 */
export function sameData(a: unknown, b: unknown): boolean {
  return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
}

/** 两份文档之间要动哪些键。两边都是非空映射的往下钻，其余整块替换 ——
 *  空映射也整块替换：逐个删光子键会留下一个解析成 null 的空键。列表带着原值，好逐项改。 */
export function diffDocuments(prev: Mapping, next: Mapping, prefix: readonly string[] = []): YamlEdit[] {
  const edits: YamlEdit[] = [];
  for (const key of Object.keys(prev)) {
    if (!has(next, key)) edits.push({ op: 'delete', path: [...prefix, key] });
  }
  for (const [key, value] of Object.entries(next)) {
    const path = [...prefix, key];
    if (!has(prev, key)) {
      edits.push({ op: 'set', path, value });
      continue;
    }
    const before = prev[key];
    if (nonEmptyMapping(before) && nonEmptyMapping(value)) {
      edits.push(...diffDocuments(before, value, path));
    } else if (!sameData(before, value)) {
      edits.push({ op: 'set', path, value, before });
    }
  }
  return edits;
}

/** 带注释的行数，整行注释和行尾注释都算。宁可多报：块标量正文里的「 #」也会被当成注释。 */
const countComments = (lines: readonly string[], from: number, to: number): number => (
  lines.slice(from, to).filter((line) => commentAfter(line) !== '').length
);

/** 新写的子项缩进多少：跟着原来第一个子项走，一个子项都没有时往里缩两格。
 *  和键同一缩进只有列表才合法（YAML 允许列表项不缩进），其余情况得往里缩。 */
function childIndent(lines: readonly string[], key: KeyLine, end: number, sequence: boolean): number {
  for (let i = key.at + 1; i < end; i += 1) {
    if (!isContent(lines[i])) continue;
    const indent = indentOf(lines[i]);
    return indent > key.indent || sequence ? indent : key.indent + 2;
  }
  return key.indent + 2;
}

/** 块里夹在键和第一个子项之间的注释，说的是这一项本身，换值时留着。 */
function leadingComments(lines: readonly string[], key: KeyLine, end: number): string[] {
  const kept: string[] = [];
  for (let i = key.at + 1; i < end; i += 1) {
    if (isContent(lines[i])) break;
    kept.push(lines[i]);
  }
  return kept;
}

interface Rendered {
  /** 跟在「键:」后面的部分；块写法的集合是空串。 */
  head: string;
  /** 键下面的行，自带相对缩进。 */
  body: string[];
  /** body 是块标量的内容，缩进相对键本身；否则是集合的子项，缩进相对子级。 */
  scalar: boolean;
}

const dumpLines = (value: unknown, options: yaml.DumpOptions): string[] => (
  yaml.dump(value, options).replace(/\n$/, '').split('\n')
);

/** inline：原来那一行就是行内取值时，只含标量的集合也写成行内，免得一个 [] 被改成一大块；
 *  里面套着映射或列表的照样写成块，挤成一行没法看。 */
function renderValue(value: unknown, options: yaml.DumpOptions, inline: boolean): Rendered {
  if (isScalar(value)) {
    const [head, ...body] = dumpLines(value, options);
    return { head, body, scalar: true };
  }
  const items = Array.isArray(value) ? value : Object.values(value as Mapping);
  if (!items.length || (inline && items.every(isScalar))) {
    const flow = dumpLines(value, { ...options, flowLevel: 0 });
    if (flow.length === 1) return { head: flow[0], body: [], scalar: false };
  }
  return { head: '', body: dumpLines(value, options), scalar: false };
}

/** 新插的键。普通标识符原样写，其余交给 dump 决定引号 —— 纯数字的键不加引号就读成数字了。 */
function renderKey(key: string, options: yaml.DumpOptions): string {
  if (PLAIN_KEY.test(key)) return key;
  const line = yaml.dump({ [key]: null }, options);
  return line.slice(0, line.lastIndexOf(':'));
}

/** 键那一行加上它下面的行。indent.key 是键本身的缩进，indent.child 是集合子项的缩进。 */
function renderEntry(
  keyText: string,
  rendered: Rendered,
  indent: { key: number; child: number },
  comment = '',
): string[] {
  const head = rendered.head ? `${keyText}: ${rendered.head}${comment}` : `${keyText}:${comment}`;
  const pad = ' '.repeat(rendered.scalar ? indent.key : indent.child);
  return [head, ...rendered.body.map((part) => `${pad}${part}`)];
}

interface ItemSpan {
  /** 「-」那一行。 */
  at: number;
  /** 这一项到哪一行为止（不含）。尾随的空行和注释说的多半是下一项，归下一项。 */
  end: number;
  /** 「-」所在的列。 */
  indent: number;
}

/** 块状列表的各项。键带行内取值（流式列表），或者块里混着认不出的行，返回 null。 */
function sequenceItems(lines: readonly string[], key: KeyLine, end: number): ItemSpan[] | null {
  if (keyParts(lines, key).value) return null;
  const starts: number[] = [];
  let indent = -1;
  for (let i = key.at + 1; i < end; i += 1) {
    if (!isContent(lines[i])) continue;
    const lineIndent = indentOf(lines[i]);
    if (indent < 0) indent = lineIndent;
    if (lineIndent < indent) return null;
    if (lineIndent === indent) {
      if (!isSequenceItem(lines[i])) return null;
      starts.push(i);
    }
  }
  return starts.map((at, n) => ({ at, indent, end: contentEnd(lines, at, starts[n + 1] ?? end) }));
}

/** 映射项往里钻：把「- 」换成空格，这一项就成了普通的块映射，拿同一套逐键改写去改，改完把「-」放回第一行内容上。
 *  「-」单独一行时那一行留着，只改下面。认不出的写法返回 null，交给整项重写。 */
function editMappingItem(
  lines: readonly string[],
  item: ItemSpan,
  before: Mapping,
  after: Mapping,
  options: yaml.DumpOptions,
): Step | null {
  const dashLine = lines[item.at];
  const afterDash = dashLine.slice(item.indent + 1);
  const bareDash = !afterDash.trim();
  const body = bareDash
    ? lines.slice(item.at + 1, item.end)
    : [`${dashLine.slice(0, item.indent)} ${afterDash}`, ...lines.slice(item.at + 1, item.end)];
  let step: Step;
  try {
    step = applyEdits(body, before, after, options);
  } catch (error) {
    if (error instanceof YamlShapeError) return null;
    throw error;
  }
  let edited = step.lines;
  if (!bareDash) {
    const first = edited.findIndex(isContent);
    if (first < 0 || indentOf(edited[first]) <= item.indent) return null;
    edited = edited.map((line, i) => (i === first ? `${line.slice(0, item.indent)}-${line.slice(item.indent + 1)}` : line));
  }
  const head = bareDash ? [dashLine] : [];
  return { lines: [...lines.slice(0, item.at), ...head, ...edited, ...lines.slice(item.end)], dropped: step.dropped };
}

/** 改一项。两边都是非空映射时往里钻，只改变了的键；否则整项重写，项里的注释算丢掉的。 */
function editItem(
  lines: readonly string[],
  item: ItemSpan,
  before: unknown,
  after: unknown,
  options: yaml.DumpOptions,
): Step {
  if (nonEmptyMapping(before) && nonEmptyMapping(after)) {
    const nested = editMappingItem(lines, item, before, after, options);
    if (nested) return nested;
  }
  const rendered = dumpLines([after], options).map((line) => `${' '.repeat(item.indent)}${line}`);
  return {
    lines: [...lines.slice(0, item.at), ...rendered, ...lines.slice(item.end)],
    dropped: countComments(lines, item.at, item.end),
  };
}

/** 列表逐项改，没变的项连同里面的注释原样不动。能就地改的是三种：项数不变且没有项挪位置、只在末尾追加、只删了一项；
 *  其余（换序、一次增删好几项）返回 null，交给整块替换。 */
function editSequence(
  lines: readonly string[],
  key: KeyLine,
  end: number,
  before: readonly unknown[],
  after: readonly unknown[],
  options: yaml.DumpOptions,
): Step | null {
  const items = sequenceItems(lines, key, end);
  if (!items || !items.length || items.length !== before.length) return null;

  if (after.length === before.length) {
    const changed = before.map((_, i) => i).filter((i) => !sameData(before[i], after[i]));
    // 新值是别的位置原来的值，就是挪了位置：逐位改写会把注释留在原处、挂到别的项上。
    if (changed.some((i) => before.some((old, j) => j !== i && sameData(old, after[i])))) return null;
    let current = [...lines];
    let dropped = 0;
    // 从后往前改：后面的项变长变短，不影响前面各项的行号。
    for (const i of changed.reverse()) {
      const step = editItem(current, items[i], before[i], after[i], options);
      current = step.lines;
      dropped += step.dropped;
    }
    return { lines: current, dropped };
  }

  if (after.length > before.length && sameData(after.slice(0, before.length), before)) {
    const last = items[items.length - 1];
    const added = dumpLines(after.slice(before.length), options).map((line) => `${' '.repeat(last.indent)}${line}`);
    return { lines: [...lines.slice(0, last.end), ...added, ...lines.slice(last.end)], dropped: 0 };
  }

  if (after.length === before.length - 1) {
    const differs = before.findIndex((item, i) => i >= after.length || !sameData(item, after[i]));
    if (!sameData([...before.slice(0, differs), ...before.slice(differs + 1)], after)) return null;
    const gone = items[differs];
    return { lines: [...lines.slice(0, gone.at), ...lines.slice(gone.end)], dropped: 0 };
  }

  return null;
}

function replaceValue(
  lines: readonly string[],
  key: KeyLine,
  before: unknown,
  value: unknown,
  options: yaml.DumpOptions,
): Step {
  const end = blockEnd(lines, key);
  if (Array.isArray(before) && Array.isArray(value)) {
    const inPlace = editSequence(lines, key, end, before, value, options);
    if (inPlace) return inPlace;
  }
  const parts = keyParts(lines, key);
  const blockScalar = isBlockScalarHead(parts.value);
  const rendered = renderValue(value, options, Boolean(parts.value) && !blockScalar);
  // 块标量的内容行里 # 开头的是正文不是注释；新值是块标量时同理，夹进去就成了正文。
  const kept = blockScalar || rendered.scalar ? [] : leadingComments(lines, key, end);
  const child = childIndent(lines, key, end, Array.isArray(value));
  const [head, ...body] = renderEntry(keyHead(lines, key), rendered, { key: key.indent, child }, parts.comment);
  return {
    lines: [...lines.slice(0, key.at), head, ...kept, ...body, ...lines.slice(end)],
    dropped: blockScalar ? 0 : countComments(lines, key.at + 1, end) - countComments(kept, 0, kept.length),
  };
}

function insertValue(
  lines: readonly string[],
  path: readonly string[],
  value: unknown,
  options: yaml.DumpOptions,
): string[] {
  let known = path.length - 1;
  let parent: KeyLine | null = null;
  while (known > 0 && !(parent = findKey(lines, path.slice(0, known)))) known -= 1;

  const next = [...lines];
  const missing = path.slice(known);
  let at: number;
  let base: number;
  if (parent) {
    openBlock(next, parent, path.slice(0, known), missing);
    at = blockEnd(next, parent);
    base = childIndent(next, parent, at, false);
  } else {
    // 最外层：接在最后一行内容后面，末尾的空行留在最后。列表项往里钻时最外层不在第 0 列，跟着第一行内容走。
    at = next.length;
    while (at > 0 && !next[at - 1].trim()) at -= 1;
    base = topIndent(next);
  }

  const block = missing.flatMap((key, depth) => {
    const indent = base + 2 * depth;
    const keyText = `${' '.repeat(indent)}${renderKey(key, options)}`;
    if (depth < missing.length - 1) return [`${keyText}:`];
    return renderEntry(keyText, renderValue(value, options, false), { key: indent, child: indent + 2 });
  });
  return [...next.slice(0, at), ...block, ...next.slice(at)];
}

function setValue(lines: readonly string[], edit: Extract<YamlEdit, { op: 'set' }>, options: yaml.DumpOptions): Step {
  const found = findKey(lines, edit.path);
  if (!found) return { lines: insertValue(lines, edit.path, edit.value, options), dropped: 0 };
  return replaceValue(lines, found, edit.before, edit.value, options);
}

function deleteKey(lines: readonly string[], path: readonly string[]): Step {
  const found = findKey(lines, path);
  if (!found) return { lines: [...lines], dropped: 0 };
  const end = blockEnd(lines, found);
  const next = [...lines.slice(0, found.at), ...lines.slice(end)];
  // 删掉的那块原本夹在两段空行之间：收成一段，免得每删一次多出一行空白。
  const at = found.at;
  if (at > 0 && at < next.length && !next[at - 1].trim() && !next[at].trim()) next.splice(at, 1);
  return { lines: next, dropped: 0 };
}

/** 把 lines 里的映射从 prev 改成 next。认不出的写法抛 YamlShapeError。 */
function applyEdits(lines: readonly string[], prev: Mapping, next: Mapping, options: yaml.DumpOptions): Step {
  let current = [...lines];
  let dropped = 0;
  for (const edit of diffDocuments(prev, next)) {
    const step = edit.op === 'delete' ? deleteKey(current, edit.path) : setValue(current, edit, options);
    current = step.lines;
    dropped += step.dropped;
  }
  return { lines: current, dropped };
}

/** 原文解析成映射。原文坏了就不写：照着坏文件合出来的结果谁也说不准。 */
function parseMapping(raw: string): Mapping {
  let loaded: unknown;
  try {
    loaded = yaml.load(raw);
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new Error(`原文不是合法的 YAML，请先修好再改，原文件未修改：${reason}`);
  }
  if (loaded === undefined || loaded === null) return {};
  if (!isMapping(loaded)) throw new Error('原文顶层不是映射（key: value），请先修好再改，原文件未修改');
  return loaded;
}

function reparsesTo(text: string, next: Mapping): boolean {
  try {
    return sameData(yaml.load(text) ?? {}, next);
  } catch {
    return false;
  }
}

/** 把 raw 改写成解析结果等于 next 的原文。options 用于渲染改动的值，也用于整份重写的兜底。 */
export function rewriteYaml(raw: string, next: Mapping, options: yaml.DumpOptions): YamlRewrite {
  const prev = parseMapping(raw);
  const whole = (): YamlRewrite => ({ content: yaml.dump(next, options), preserved: false, note: WHOLE_FILE_REWRITTEN });
  const { lines, eol } = splitLines(raw);
  let step: Step;
  try {
    step = applyEdits(lines, prev, next, options);
  } catch (error) {
    if (error instanceof YamlShapeError) return whole();
    throw error;
  }
  const content = step.lines.join(eol);
  if (!reparsesTo(content, next)) return whole();
  return step.dropped
    ? { content, preserved: false, note: BLOCK_COMMENTS_DROPPED }
    : { content, preserved: true, note: '' };
}
