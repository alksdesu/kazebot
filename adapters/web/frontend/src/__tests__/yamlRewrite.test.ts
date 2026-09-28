// 把改过的文档写回 yaml 原文。改坏一次，运营者写在配置里的注释就整份没了，
// 所以每条路径都钉住落地的那几行原文。
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import yaml from 'js-yaml';
import { describe, expect, it } from 'vitest';

import {
  BLOCK_COMMENTS_DROPPED,
  WHOLE_FILE_REWRITTEN,
  diffDocuments,
  rewriteYaml,
  sameData,
} from '../console/yamlRewrite';

// 和 consoleStore 写配置时用的一套参数一致。
const OPTS: yaml.DumpOptions = {
  indent: 2,
  lineWidth: -1,
  noRefs: true,
  sortKeys: false,
  quotingType: '"',
  forceQuotes: false,
};

type Doc = Record<string, any>;

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../..');
const read = (rel: string): string => readFileSync(resolve(REPO, rel), 'utf-8');

const rows = (text: string): string[] => text.split(/\r?\n/);
const load = (raw: string): Doc => yaml.load(raw) as Doc;
const clone = (doc: Doc): Doc => JSON.parse(JSON.stringify(doc)) as Doc;

const commentRows = (text: string): number => rows(text).filter((row) => row.trimStart().startsWith('#')).length;
const blankRows = (text: string): number => rows(text).filter((row) => !row.trim()).length;

/** 原文和改后之间那一段连续的增删改。头尾两侧逐字相同，所以断言了 removed/added
 *  就等于断言了「其余行一个字符都没动」。 */
function soleEdit(before: string, after: string): { at: number; removed: string[]; added: string[] } {
  const a = rows(before);
  const b = rows(after);
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (
    tail < a.length - head
    && tail < b.length - head
    && a[a.length - 1 - tail] === b[b.length - 1 - tail]
  ) tail += 1;
  return { at: head, removed: a.slice(head, a.length - tail), added: b.slice(head, b.length - tail) };
}

/** 改一处再落回原文，顺手把每个场景都要成立的三件事查掉：保住原文、注释和空行没少、解析结果就是目标。 */
function applyEdit(raw: string, mutate: (doc: Doc) => void): { content: string; at: number; removed: string[]; added: string[] } {
  const doc = clone(load(raw));
  mutate(doc);
  const { content, preserved, note } = rewriteYaml(raw, doc, OPTS);

  expect({ preserved, note }).toEqual({ preserved: true, note: '' });
  expect(yaml.load(content)).toEqual(doc);
  expect(commentRows(content)).toBe(commentRows(raw));
  expect(blankRows(content)).toBe(blankRows(raw));
  return { content, ...soleEdit(raw, content) };
}

const EXAMPLES = [
  'defaults/qq.example.yaml',
  'defaults/features.example.yaml',
  'tools/drawtools/settings.example.yaml',
];

describe('rewriteYaml on the shipped example configs', () => {
  it.each(EXAMPLES)('gives back %s byte for byte when nothing changed', (rel) => {
    const raw = read(rel);

    expect(rewriteYaml(raw, load(raw), OPTS)).toEqual({ content: raw, preserved: true, note: '' });
  });
});

describe('rewriteYaml against defaults/qq.example.yaml', () => {
  const raw = read('defaults/qq.example.yaml');

  it('changes one scalar and nothing else', () => {
    const edit = applyEdit(raw, (doc) => { doc.output.message_limit = 5000; });

    expect(edit.removed).toEqual(['  message_limit: 4300']);
    expect(edit.added).toEqual(['  message_limit: 5000']);
  });

  it('fills an inline [] and keeps it inline', () => {
    // 一个 [] 被摊成分行列表，下面那行 `#   - 123456789` 的示例就跟内容对不上了。
    const edit = applyEdit(raw, (doc) => { doc.channels.allowed_groups = [123456789, 987654321]; });

    expect(edit.removed).toEqual(['  allowed_groups: []']);
    expect(edit.added).toEqual(['  allowed_groups: [123456789, 987654321]']);
  });

  it('keeps the trailing comment when the value on that line changes', () => {
    const edit = applyEdit(raw, (doc) => { doc.extensions.sticker.groups = [111]; });

    expect(edit.removed).toEqual(['    groups: []              # 只从这些群收，留空表示所有已授权的群']);
    expect(edit.added).toEqual(['    groups: [111]              # 只从这些群收，留空表示所有已授权的群']);
  });

  it('deletes a key by dropping its line only', () => {
    const edit = applyEdit(raw, (doc) => { delete doc.input.forward_msg_max_depth; });

    expect(edit.removed).toEqual(['  forward_msg_max_depth: 3']);
    expect(edit.added).toEqual([]);
  });

  it('inserts a nested key at the end of its parent block', () => {
    const edit = applyEdit(raw, (doc) => { doc.extensions.sticker.filters = { min_width: 100 }; });

    expect(edit.removed).toEqual([]);
    expect(edit.added).toEqual(['    filters:', '      min_width: 100']);
    // 插在 sticker 块的末尾，后面那段 `# ── 输入能力 ──` 的说明还在下面。
    expect(rows(edit.content)[edit.at - 1]).toBe('    lookup_order: native_first');
    expect(rows(edit.content)[edit.at + 2]).toBe('');
  });
});

describe('rewriteYaml against defaults/features.example.yaml', () => {
  const raw = read('defaults/features.example.yaml');

  it('changes one scalar and nothing else', () => {
    const edit = applyEdit(raw, (doc) => { doc.trigger.random.probability = 0.5; });

    expect(edit.removed).toEqual(['    probability: 0.02']);
    expect(edit.added).toEqual(['    probability: 0.5']);
  });

  it('rewrites a block list at the indent it already had', () => {
    const edit = applyEdit(raw, (doc) => { doc.trigger.prefixes = ['!', '#']; });

    expect(edit.removed).toEqual(['    - "！"', '    - "/"', '    - "／"']);
    expect(edit.added).toEqual(['    - "#"']);
  });

  it('fills an inline [] and keeps it inline', () => {
    const edit = applyEdit(raw, (doc) => { doc.trigger.keyword.words = ['天气', '新闻']; });

    expect(edit.removed).toEqual(['    words: []']);
    expect(edit.added).toEqual(['    words: [天气, 新闻]']);
  });

  it('writes a value onto a line whose comment must survive', () => {
    const edit = applyEdit(raw, (doc) => { doc.trigger.signals.all = true; });

    expect(edit.removed).toEqual(['    all: null           # 群内任意消息都回']);
    expect(edit.added).toEqual(['    all: true           # 群内任意消息都回']);
  });

  it('deletes a whole nested block without touching the comment after it', () => {
    const edit = applyEdit(raw, (doc) => { delete doc.extensions.echo; });

    expect(edit.removed).toEqual([
      '  echo:',
      '    enabled: false',
      '    threshold: 3          # 连续几条相同才跟，必须来自不同的人',
      '    max_text_length: 30   # 超过这个长度不跟',
      '    window_sec: 120.0     # 复读要在这个时间窗内连续发生',
      '    cooldown_sec: 90.0    # 同群两次跟读之间至少隔这么久',
    ]);
    expect(edit.added).toEqual([]);
    expect(rows(edit.content)[edit.at]).toBe('  # 表情包。收不收、从哪收、收多严是各平台自己的设置，写在平台文件里。');
  });

  it('inserts a nested key that needs its parent level built', () => {
    const edit = applyEdit(raw, (doc) => { doc.queue.retry = { max_attempts: 3 }; });

    expect(edit.removed).toEqual([]);
    expect(edit.added).toEqual(['  retry:', '    max_attempts: 3']);
    expect(rows(edit.content)[edit.at - 1]).toBe('  reply_timeout_sec: 120.0');
  });
});

describe('rewriteYaml against tools/drawtools/settings.example.yaml', () => {
  const raw = read('tools/drawtools/settings.example.yaml');

  it('edits a preset without indenting the list PyYAML wrote flat', () => {
    const doc = clone(load(raw));
    doc.params.presets[0].params.steps = 30;

    const { content, preserved, note } = rewriteYaml(raw, doc, OPTS);

    expect({ preserved, note }).toEqual({ preserved: true, note: '' });
    expect(yaml.load(content)).toEqual(doc);
    expect(commentRows(content)).toBe(commentRows(raw));
    // 前后两头得逐字不动 —— 三段说明清理和重试规则的注释都在前面。
    const before = rows(raw);
    const after = rows(content);
    const head = before.indexOf('  presets:');
    expect(after.slice(0, head + 1)).toEqual(before.slice(0, head + 1));
    expect(after.slice(after.indexOf('character_tags:'))).toEqual(before.slice(before.indexOf('character_tags:')));
    expect(after.filter((row) => row.startsWith('  - id:'))).toEqual([
      '  - id: default-v45-full',
      '  - id: cute-v45',
    ]);
    expect(after.filter((row) => row.trim().startsWith('steps:'))).toEqual(['      steps: 30', '      steps: 28']);
  });

  it('changes a field in each of the two presets and moves no other line', () => {
    const doc = clone(load(raw));
    doc.params.presets[0].params.steps = 30;
    doc.params.presets[1].params.scale = 7;

    const { content, preserved, note } = rewriteYaml(raw, doc, OPTS);

    expect({ preserved, note }).toEqual({ preserved: true, note: '' });
    expect(yaml.load(content)).toEqual(doc);
    const before = rows(raw);
    const after = rows(content);
    expect(after.length).toBe(before.length);
    const changed = after
      .map((row, i): [number, string, string] => [i, before[i], row])
      .filter(([, was, now]) => was !== now);
    expect(changed).toEqual([
      [before.indexOf('      steps: 28'), '      steps: 28', '      steps: 30'],
      [before.indexOf('      scale: 5.5'), '      scale: 5.5', '      scale: 7'],
    ]);
  });

  it('keeps the file on CRLF', () => {
    const doc = clone(load(raw));
    doc.storage.retention_days = 14;

    const { content } = rewriteYaml(raw, doc, OPTS);

    expect(content.includes('\r\n')).toBe(true);
    expect(/[^\r]\n/.test(content)).toBe(false);
    expect(soleEdit(raw, content)).toEqual({
      at: rows(raw).indexOf('  retention_days: 7'),
      removed: ['  retention_days: 7'],
      added: ['  retention_days: 14'],
    });
  });
});

describe('rewriteYaml on a block list', () => {
  // 三项，第一项里夹着一句说明，第三项头上有一句说明。
  const CHARACTERS = [
    'characters:',
    '  - name: A',
    '    # A 的说明',
    '    danbooru: a',
    '  - name: B',
    '    danbooru: b',
    '  # 下面这位是新来的',
    '  - name: C',
    '    danbooru: c',
    '',
  ].join('\n');

  it('keeps a comment that sits inside the item being changed', () => {
    const raw = 'characters:\n  - name: A\n    # 给下一个人看的\n    danbooru: a\n';

    const out = rewriteYaml(raw, { characters: [{ name: 'A', danbooru: 'b' }] }, { lineWidth: 120, noRefs: true });

    expect(out).toEqual({
      content: 'characters:\n  - name: A\n    # 给下一个人看的\n    danbooru: b\n',
      preserved: true,
      note: '',
    });
  });

  it('rewrites only the item whose field changed', () => {
    const next = {
      characters: [
        { name: 'A', danbooru: 'a' },
        { name: 'B', danbooru: 'bb' },
        { name: 'C', danbooru: 'c' },
      ],
    };

    expect(rewriteYaml(CHARACTERS, next, OPTS)).toEqual({
      content: [
        'characters:',
        '  - name: A',
        '    # A 的说明',
        '    danbooru: a',
        '  - name: B',
        '    danbooru: bb',
        '  # 下面这位是新来的',
        '  - name: C',
        '    danbooru: c',
        '',
      ].join('\n'),
      preserved: true,
      note: '',
    });
  });

  it('adds a key inside one item and leaves the rest of the block alone', () => {
    const next = {
      characters: [
        { name: 'A', danbooru: 'a', tag: 'x' },
        { name: 'B', danbooru: 'b' },
        { name: 'C', danbooru: 'c' },
      ],
    };

    expect(rewriteYaml(CHARACTERS, next, OPTS)).toEqual({
      content: [
        'characters:',
        '  - name: A',
        '    # A 的说明',
        '    danbooru: a',
        '    tag: x',
        '  - name: B',
        '    danbooru: b',
        '  # 下面这位是新来的',
        '  - name: C',
        '    danbooru: c',
        '',
      ].join('\n'),
      preserved: true,
      note: '',
    });
  });

  it('appends a new item after the last one', () => {
    const next = {
      characters: [
        { name: 'A', danbooru: 'a' },
        { name: 'B', danbooru: 'b' },
        { name: 'C', danbooru: 'c' },
        { name: 'D', danbooru: 'd' },
      ],
    };

    expect(rewriteYaml(CHARACTERS, next, OPTS)).toEqual({
      content: [
        'characters:',
        '  - name: A',
        '    # A 的说明',
        '    danbooru: a',
        '  - name: B',
        '    danbooru: b',
        '  # 下面这位是新来的',
        '  - name: C',
        '    danbooru: c',
        '  - name: D',
        '    danbooru: d',
        '',
      ].join('\n'),
      preserved: true,
      note: '',
    });
  });

  it('drops only the lines of the item that went away', () => {
    const next = { characters: [{ name: 'A', danbooru: 'a' }, { name: 'C', danbooru: 'c' }] };

    expect(rewriteYaml(CHARACTERS, next, OPTS)).toEqual({
      content: [
        'characters:',
        '  - name: A',
        '    # A 的说明',
        '    danbooru: a',
        '  # 下面这位是新来的',
        '  - name: C',
        '    danbooru: c',
        '',
      ].join('\n'),
      preserved: true,
      note: '',
    });
  });

  it('replaces the block whole when the items change order, and says the comments went', () => {
    const next = { characters: [{ name: 'C', danbooru: 'c' }, { name: 'A', danbooru: 'a' }] };

    const out = rewriteYaml(CHARACTERS, next, OPTS);

    expect(out).toEqual({
      content: 'characters:\n  - name: C\n    danbooru: c\n  - name: A\n    danbooru: a\n',
      preserved: false,
      note: BLOCK_COMMENTS_DROPPED,
    });
    expect(yaml.load(out.content)).toEqual(next);
  });

  it('replaces the block whole when two items go at once', () => {
    const next = { characters: [{ name: 'B', danbooru: 'b' }] };

    const out = rewriteYaml(CHARACTERS, next, OPTS);

    expect(out).toEqual({
      content: 'characters:\n  - name: B\n    danbooru: b\n',
      preserved: false,
      note: BLOCK_COMMENTS_DROPPED,
    });
    expect(yaml.load(out.content)).toEqual(next);
  });

  it('replaces the block whole when the items only moved, rather than leaving comments on the wrong ones', () => {
    // 逐位改写会把「A 的说明」原地留给挪过来的 C，注释就此指错了人。
    const next = {
      characters: [
        { name: 'C', danbooru: 'c' },
        { name: 'A', danbooru: 'a' },
        { name: 'B', danbooru: 'b' },
      ],
    };

    const out = rewriteYaml(CHARACTERS, next, OPTS);

    expect(out).toEqual({
      content: [
        'characters:',
        '  - name: C',
        '    danbooru: c',
        '  - name: A',
        '    danbooru: a',
        '  - name: B',
        '    danbooru: b',
        '',
      ].join('\n'),
      preserved: false,
      note: BLOCK_COMMENTS_DROPPED,
    });
    expect(yaml.load(out.content)).toEqual(next);
  });

  it('still rewrites one item in place when its new value is not another item', () => {
    // 只有「新值等于别处的旧值」才算挪位置，寻常改值仍然只动那一项。
    const next = {
      characters: [
        { name: 'A', danbooru: 'a' },
        { name: 'B', danbooru: 'b' },
        { name: 'D', danbooru: 'd' },
      ],
    };

    expect(rewriteYaml(CHARACTERS, next, OPTS)).toEqual({
      content: [
        'characters:',
        '  - name: A',
        '    # A 的说明',
        '    danbooru: a',
        '  - name: B',
        '    danbooru: b',
        '  # 下面这位是新来的',
        '  - name: D',
        '    danbooru: d',
        '',
      ].join('\n'),
      preserved: true,
      note: '',
    });
  });

  it('falls back to a whole-block rewrite when the new value equals another entry that was already there', () => {
    // 表里本来就有重复值时分不清是挪位置还是改值，宁可整块重写也不把注释留给错的人。
    const raw = 'words:\n  - a   # 第一个\n  - b\n  - a\n';

    const out = rewriteYaml(raw, { words: ['a', 'a', 'a'] }, OPTS);

    expect(out).toEqual({
      content: 'words:\n  - a\n  - a\n  - a\n',
      preserved: false,
      note: BLOCK_COMMENTS_DROPPED,
    });
  });

  it('drops the comment trailing the item it rewrites, and says so', () => {
    // 键的行尾注释说的是键，列表项的行尾注释说的多半是那个值，换了值就不该跟着走。
    const out = rewriteYaml('presets:\n  - a   # 第一个\n  - b\n', { presets: ['c', 'b'] }, OPTS);

    expect(out).toEqual({
      content: 'presets:\n  - c\n  - b\n',
      preserved: false,
      note: BLOCK_COMMENTS_DROPPED,
    });
  });

  it('counts a trailing comment lost to a whole-block rewrite', () => {
    const out = rewriteYaml('presets:\n  - a   # 第一个\n  - b\n', { presets: ['z', 'a', 'b'] }, OPTS);

    expect(out).toEqual({
      content: 'presets:\n  - z\n  - a\n  - b\n',
      preserved: false,
      note: BLOCK_COMMENTS_DROPPED,
    });
  });

  it('counts a trailing comment lost when a mapping block becomes a list', () => {
    const out = rewriteYaml('a:\n  b: 1   # 说明\nz: 1\n', { a: [1], z: 1 }, OPTS);

    expect(out).toEqual({
      content: 'a:\n  - 1\nz: 1\n',
      preserved: false,
      note: BLOCK_COMMENTS_DROPPED,
    });
  });

  it('would rather over-report than miss one: a # inside a block scalar counts too', () => {
    const out = rewriteYaml('a:\n  p: |\n    hello # world\nz: 1\n', { a: [1], z: 1 }, OPTS);

    expect(out).toEqual({
      content: 'a:\n  - 1\nz: 1\n',
      preserved: false,
      note: BLOCK_COMMENTS_DROPPED,
    });
  });

  it('keeps the trailing comment when it can edit inside the item instead', () => {
    // 往里钻只改变了的那个键，同一行的说明原样留着。
    expect(rewriteYaml('c:\n  - n: A   # 说明\n    v: 1\n', { c: [{ n: 'A', v: 2 }] }, OPTS)).toEqual({
      content: 'c:\n  - n: A   # 说明\n    v: 2\n',
      preserved: true,
      note: '',
    });
  });

  it('writes a first item as a block instead of squeezing it onto one line', () => {
    const out = rewriteYaml(
      'params:\n  presets: []\n  n: 1\n',
      { params: { presets: [{ id: 'x', steps: 1 }], n: 1 } },
      OPTS,
    );

    expect(out).toEqual({
      content: 'params:\n  presets:\n    - id: x\n      steps: 1\n  n: 1\n',
      preserved: true,
      note: '',
    });
  });
});

describe('rewriteYaml shapes', () => {
  it('keeps CRLF on a hand-written file', () => {
    const out = rewriteYaml('a: 1\r\n# 说明\r\nb: 2\r\n', { a: 9, b: 2 }, OPTS);

    expect(out).toEqual({ content: 'a: 9\r\n# 说明\r\nb: 2\r\n', preserved: true, note: '' });
  });

  it('writes a Date as a leaf instead of walking into it', () => {
    const out = rewriteYaml('at: 2026-01-01\nn: 1\n', { at: new Date(Date.UTC(2030, 0, 2)), n: 1 }, OPTS);

    expect(out).toEqual({ content: 'at: 2030-01-02T00:00:00.000Z\nn: 1\n', preserved: true, note: '' });
  });

  it('leaves an unchanged Date alone', () => {
    const raw = 'at: 2026-01-01\n';

    expect(rewriteYaml(raw, { at: new Date(Date.UTC(2026, 0, 1)) }, OPTS))
      .toEqual({ content: raw, preserved: true, note: '' });
  });

  it('quotes a number-shaped key so it does not read back as a number', () => {
    const out = rewriteYaml('a:\n  x: 1\n', { a: { x: 1, 2024: 'jan' } }, OPTS);

    expect(out).toEqual({ content: 'a:\n  x: 1\n  "2024": jan\n', preserved: true, note: '' });
  });

  it('writes into a quoted key without unquoting it', () => {
    const out = rewriteYaml('"2024": jan  # note\nn: 1\n', { 2024: 'feb', n: 1 }, OPTS);

    expect(out).toEqual({ content: '"2024": feb  # note\nn: 1\n', preserved: true, note: '' });
  });

  it('writes into a key that holds a colon', () => {
    const out = rewriteYaml('a:b: 1\nn: 2\n', { 'a:b': 9, n: 2 }, OPTS);

    expect(out).toEqual({ content: 'a:b: 9\nn: 2\n', preserved: true, note: '' });
  });

  it('replaces a block scalar whole', () => {
    const out = rewriteYaml('prompt: |\n  old one\n  old two\nnext: 1\n', { prompt: 'new one\nnew two\n', next: 1 }, OPTS);

    expect(out).toEqual({ content: 'prompt: |\n  new one\n  new two\nnext: 1\n', preserved: true, note: '' });
  });

  it('fills a {} placeholder and keeps the comment on that line', () => {
    const out = rewriteYaml('a: {}   # 说明\nb: 1\n', { a: { c: 2 }, b: 1 }, OPTS);

    expect(out).toEqual({ content: 'a: {c: 2}   # 说明\nb: 1\n', preserved: true, note: '' });
  });

  it('keeps an inline list inline while it holds nothing but scalars', () => {
    const out = rewriteYaml('a: [1, 2]\nz: 2\n', { a: [1, 2, 3], z: 2 }, OPTS);

    expect(out).toEqual({ content: 'a: [1, 2, 3]\nz: 2\n', preserved: true, note: '' });
  });

  it('keeps an emptied inline list on its line', () => {
    const out = rewriteYaml('a: [1, 2]\nz: 2\n', { a: [], z: 2 }, OPTS);

    expect(out).toEqual({ content: 'a: []\nz: 2\n', preserved: true, note: '' });
  });

  it('spells an inline list out as a block once it holds a mapping', () => {
    // 挤成一行就成了 `a: [{b: 1}]`，没法看也没法改。
    const out = rewriteYaml('a: [1]\nz: 2\n', { a: [{ b: 1 }], z: 2 }, OPTS);

    expect(out).toEqual({ content: 'a:\n  - b: 1\nz: 2\n', preserved: true, note: '' });
  });

  it('appends a top-level key that was never there', () => {
    expect(rewriteYaml('a: 1\n', { a: 1, b: 2 }, OPTS)).toEqual({ content: 'a: 1\nb: 2\n', preserved: true, note: '' });
  });

  it('lines a new top-level key up with the first content line', () => {
    expect(rewriteYaml('  a: 1\n  b: 2\n', { a: 1, b: 2, c: 3 }, OPTS))
      .toEqual({ content: '  a: 1\n  b: 2\n  c: 3\n', preserved: true, note: '' });
  });

  it('writes into an empty file', () => {
    expect(rewriteYaml('', { a: 1 }, OPTS)).toEqual({ content: 'a: 1\n', preserved: true, note: '' });
  });

  it('does not leave two blank lines where the deleted block was', () => {
    expect(rewriteYaml('a: 1\n\nb: 2\n\nc: 3\n', { a: 1, c: 3 }, OPTS)).toEqual({
      content: 'a: 1\n\nc: 3\n',
      preserved: true,
      note: '',
    });
  });

  it('dumps the whole file when a child has to go under a flow mapping', () => {
    const next = { a: { b: 1, c: 2 }, z: 2 };

    const out = rewriteYaml('a: {b: 1}\n# 说明\nz: 2\n', next, OPTS);

    expect(out).toEqual({ content: 'a:\n  b: 1\n  c: 2\nz: 2\n', preserved: false, note: WHOLE_FILE_REWRITTEN });
    expect(yaml.load(out.content)).toEqual(next);
  });

  it('refuses a broken original instead of merging onto it', () => {
    expect(() => rewriteYaml('a: [1\n', {}, OPTS)).toThrow(/^原文不是合法的 YAML，请先修好再改，原文件未修改：/);
  });

  it('refuses a top level that is not a mapping', () => {
    const message = '原文顶层不是映射（key: value），请先修好再改，原文件未修改';

    expect(() => rewriteYaml('- 1\n- 2\n', {}, OPTS)).toThrowError(new Error(message));
    expect(() => rewriteYaml('just a string\n', {}, OPTS)).toThrowError(new Error(message));
  });
});

describe('diffDocuments', () => {
  it('reports the deletes before the sets', () => {
    expect(diffDocuments({ a: 1, b: 2 }, { a: 1, c: 3 })).toEqual([
      { op: 'delete', path: ['b'] },
      { op: 'set', path: ['c'], value: 3 },
    ]);
  });

  it('carries the old value along on a key that was already there', () => {
    // 列表要逐项改，得知道原来的每一项是什么。
    expect(diffDocuments({ a: 1 }, { a: 2 })).toEqual([{ op: 'set', path: ['a'], value: 2, before: 1 }]);
  });

  it('leaves before off a key that is brand new', () => {
    const [edit] = diffDocuments({}, { a: 1 });

    expect(edit).toEqual({ op: 'set', path: ['a'], value: 1 });
    expect(edit).not.toHaveProperty('before');
  });

  it('drills into a nested mapping instead of replacing the parent', () => {
    expect(diffDocuments({ a: { x: 1, y: 2 } }, { a: { x: 9, y: 2 } })).toEqual([
      { op: 'set', path: ['a', 'x'], value: 9, before: 1 },
    ]);
  });

  it('replaces an empty mapping whole', () => {
    // 从空映射逐个加子键要先把 `a: {}` 拆成块写法，那条路认不出来，整块换掉才稳。
    expect(diffDocuments({ a: {} }, { a: { x: 1 } })).toEqual([
      { op: 'set', path: ['a'], value: { x: 1 }, before: {} },
    ]);
  });

  it('replaces a mapping that went empty whole', () => {
    // 逐个删子键会留下一个解析成 null 的空键。
    expect(diffDocuments({ a: { x: 1 } }, { a: {} })).toEqual([
      { op: 'set', path: ['a'], value: {}, before: { x: 1 } },
    ]);
  });

  it('hands the list over with both its versions', () => {
    expect(diffDocuments({ a: [1, 2] }, { a: [1, 3] })).toEqual([
      { op: 'set', path: ['a'], value: [1, 3], before: [1, 2] },
    ]);
  });

  it('treats a Date as a leaf', () => {
    const before = new Date(Date.UTC(2026, 0, 1));
    const after = new Date(Date.UTC(2030, 0, 2));

    expect(diffDocuments({ at: before }, { at: after })).toEqual([
      { op: 'set', path: ['at'], value: after, before },
    ]);
  });

  it('says nothing when the two documents match', () => {
    expect(diffDocuments({ a: { x: [1, 2] } }, { a: { x: [1, 2] } })).toEqual([]);
  });
});

describe('sameData', () => {
  it('ignores key order in mappings', () => {
    expect(sameData({ a: 1, b: { c: 2, d: 3 } }, { b: { d: 3, c: 2 }, a: 1 })).toBe(true);
  });

  it('respects list order', () => {
    expect(sameData([1, 2], [2, 1])).toBe(false);
  });

  it('tells a number from its text', () => {
    expect(sameData({ a: 1 }, { a: '1' })).toBe(false);
  });

  it('matches two dates at the same instant', () => {
    expect(sameData(new Date(Date.UTC(2026, 0, 1)), new Date(Date.UTC(2026, 0, 1)))).toBe(true);
  });
});

describe('the notes handed back to the caller', () => {
  it('says what happened when the file was rewritten whole', () => {
    expect(WHOLE_FILE_REWRITTEN).toBe('原文里有认不出的写法，这次按整份重写，文件里的注释和空行没能保留');
  });

  it('says what happened when a block was rewritten whole', () => {
    expect(BLOCK_COMMENTS_DROPPED).toBe('改动的那几项是整块重写的，块里原有的注释没能保留');
  });

  it.each([WHOLE_FILE_REWRITTEN, BLOCK_COMMENTS_DROPPED])('ends %s without punctuation so the caller can append its own', (note) => {
    expect(/[。！？.!?]$/.test(note)).toBe(false);
  });
});
