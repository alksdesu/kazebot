// yaml 原文的逐行定位。这一层量错一行，上面的定点编辑就改到别人的块里去了，
// 而且改完还是一份合法 yaml，没人看得出来。
import { describe, expect, it } from 'vitest';

import {
  EMPTY_PLACEHOLDERS,
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
  locate,
  openBlock,
  splitLines,
  topIndent,
} from '../console/yamlText';

describe('findKey', () => {
  const NESTED_FIRST = [
    'providers:',
    '  openai:',
    '    model: 嵌套的',
    'model: 顶层的',
  ];

  it('picks the top-level key even when a nested one comes first', () => {
    // 深处的同名键先出现，认错了就把 providers.openai.model 当成顶层 model 改掉。
    expect(findKey(NESTED_FIRST, ['model'])).toEqual({ at: 3, indent: 0, keyText: 'model' });
  });

  it('walks down to a direct child', () => {
    expect(findKey(NESTED_FIRST, ['providers', 'openai', 'model'])).toEqual({ at: 2, indent: 4, keyText: 'model' });
  });

  it('does not take a grandchild for a child', () => {
    expect(findKey(NESTED_FIRST, ['providers', 'model'])).toBeNull();
  });

  it('takes the child indent from the first content line, not from a comment', () => {
    expect(findKey(['a:', '  # 说明', '  b: 1'], ['a', 'b'])).toEqual({ at: 2, indent: 2, keyText: 'b' });
  });

  it('stops at the end of the parent block', () => {
    expect(findKey(['a:', '  b: 1', 'c:', '  d: 2'], ['a', 'd'])).toBeNull();
  });

  it('does not reach into a block scalar body', () => {
    // prompt 正文里写了 `  name: x`，那是用户的提示词，不是配置键。
    expect(findKey(['prompt: |', '  name: x'], ['name'])).toBeNull();
  });

  it('finds the real key that follows a block scalar with the same name inside', () => {
    expect(findKey(['prompt: |', '  name: x', 'name: 真的名字'], ['name'])).toEqual({ at: 2, indent: 0, keyText: 'name' });
  });

  it('matches the whole key, not a prefix of it', () => {
    expect(findKey(['model_name: x'], ['model'])).toBeNull();
  });

  it('finds a double-quoted key and reports how it was spelled', () => {
    // 数字键在文件里写成 `"2024":`，认不出来就会在下面再插一个同名键。
    expect(findKey(['"2024": jan', 'n: 1'], ['2024'])).toEqual({ at: 0, indent: 0, keyText: '"2024"' });
  });

  it('finds a single-quoted key and keeps that spelling', () => {
    expect(findKey(["'2024': jan"], ['2024'])).toEqual({ at: 0, indent: 0, keyText: "'2024'" });
  });

  it('finds a quoted key nested under a plain one', () => {
    expect(findKey(['a:', '  "2024": jan'], ['a', '2024'])).toEqual({ at: 1, indent: 2, keyText: '"2024"' });
  });

  it('finds a key whose name holds a colon', () => {
    expect(findKey(['a:b: 1'], ['a:b'])).toEqual({ at: 0, indent: 0, keyText: 'a:b' });
  });

  it('does not take `a:b: 1` for the key a', () => {
    expect(findKey(['a:b: 1'], ['a'])).toBeNull();
  });

  it('returns null for a key that is not there', () => {
    expect(findKey(NESTED_FIRST, ['nope'])).toBeNull();
  });
});

describe('locate', () => {
  it('throws with the full path when the key is missing', () => {
    const boom = (): unknown => locate(['a:'], ['a', 'b']);

    expect(boom).toThrow(YamlShapeError);
    expect(boom).toThrowError(new YamlShapeError('yaml 里找不到 a.b'));
  });
});

describe('keyParts', () => {
  const parts = (lines: string[], path: string[]): { value: string; comment: string } => (
    keyParts(lines, locate(lines, path))
  );

  it('splits the value from the trailing comment', () => {
    expect(parts(['model: gpt-4o  # 主模型'], ['model'])).toEqual({ value: 'gpt-4o', comment: '  # 主模型' });
  });

  it('is empty for a block-style key', () => {
    expect(parts(['tool_access:'], ['tool_access'])).toEqual({ value: '', comment: '' });
  });

  it('does not take a trailing comment for a value', () => {
    // `engine:  # 说明` 是块写法；当成「已经有值」就再也往它下面写不进去了。
    expect(parts(['engine:  # 说明'], ['engine'])).toEqual({ value: '', comment: '  # 说明' });
  });

  it('keeps the block scalar head', () => {
    expect(parts(['prompt: |'], ['prompt'])).toEqual({ value: '|', comment: '' });
  });

  it('keeps the quotes around the value', () => {
    expect(parts(['name: "QQ 综合入口"'], ['name'])).toEqual({ value: '"QQ 综合入口"', comment: '' });
  });

  it('keeps a flow mapping whole', () => {
    expect(parts(['a: {b: 1}  # c'], ['a'])).toEqual({ value: '{b: 1}', comment: '  # c' });
  });

  it('cuts by the key text, not by the first colon', () => {
    // 按第一个冒号切的话，`a:b: 1` 的取值会变成 `b: 1`。
    expect(parts(['a:b: 1'], ['a:b'])).toEqual({ value: '1', comment: '' });
  });

  it('cuts by the key text for a quoted key that holds a colon', () => {
    expect(parts(['  "x:y": 1   # c'], ['x:y'])).toEqual({ value: '1', comment: '   # c' });
  });
});

describe('keyHead', () => {
  const head = (lines: string[], path: string[]): string => keyHead(lines, locate(lines, path));

  it('is the indent plus the key', () => {
    expect(head(['  tool_mode: json   # 注释'], ['tool_mode'])).toBe('  tool_mode');
  });

  it('keeps a double-quoted key quoted', () => {
    // 改一次值就把 `"2024"` 写成 `2024`，这个键下次就读成数字了。
    expect(head(['"2024": jan  # note'], ['2024'])).toBe('"2024"');
  });

  it('keeps a single-quoted key quoted', () => {
    expect(head(["'2024': jan"], ['2024'])).toBe("'2024'");
  });

  it('keeps a colon inside the key', () => {
    expect(head(['  a:b: 1'], ['a:b'])).toBe('  a:b');
  });
});

describe('blockEnd', () => {
  const endOf = (lines: string[], path: string[]): number => blockEnd(lines, locate(lines, path));

  it('keeps an unindented list with its key', () => {
    // PyYAML 默认就这么写：列表项和键同一缩进，仍然是这个键的内容。
    expect(endOf(['presets:', '- one', '- two', 'next: 1'], ['presets'])).toBe(3);
  });

  it('counts a bare dash as an item', () => {
    expect(endOf(['a:', '-', 'b: 1'], ['a'])).toBe(2);
  });

  it('does not claim same-indent dashes when the key has an inline value', () => {
    expect(endOf(['a: 1', '- x', 'b: 2'], ['a'])).toBe(1);
  });

  it('hands the trailing blank line and comment to the next block', () => {
    // 不还回去的话，每存一次这一块就多吞两行，下一块的说明会被慢慢吃光。
    expect(endOf(['a:', '  x: 1', '', '# 下一块的说明', 'b: 2'], ['a'])).toBe(2);
  });

  it('keeps a comment that sits between two entries', () => {
    expect(endOf(['a:', '  x: 1', '  # 夹在中间', '  y: 2', 'b: 2'], ['a'])).toBe(4);
  });

  it('treats a # line inside a block scalar as body', () => {
    expect(endOf(['prompt: |', '  # 这行是正文', '  hello', 'next: 1'], ['prompt'])).toBe(3);
  });

  it('measures a nested block from its own indent', () => {
    expect(endOf(['a:', '  b:', '    x: 1', '  c: 2'], ['a', 'b'])).toBe(3);
  });

  it('runs to the end of the file', () => {
    expect(endOf(['a:', '  x: 1'], ['a'])).toBe(2);
  });

  it('measures a quoted key the same way', () => {
    expect(endOf(['"2024":', '  x: 1', 'n: 2'], ['2024'])).toBe(2);
  });
});

describe('contentEnd', () => {
  it('gives back the last content line plus one', () => {
    expect(contentEnd(['a:', '  x: 1', '', '# 下一块的说明'], 0, 4)).toBe(2);
  });

  it('leaves a range that ends on content alone', () => {
    expect(contentEnd(['a:', '  x: 1', '  y: 2'], 0, 3)).toBe(3);
  });

  it('collapses to from when the range holds nothing but blanks and comments', () => {
    expect(contentEnd(['a:', '', '# 说明', '  '], 1, 4)).toBe(1);
  });

  it('is from for an empty range', () => {
    expect(contentEnd(['a:'], 1, 1)).toBe(1);
  });
});

describe('topIndent', () => {
  it('takes the indent of the first content line', () => {
    expect(topIndent(['# 说明', '', '  a: 1', 'b: 2'])).toBe(2);
  });

  it('is zero at the left margin', () => {
    expect(topIndent(['a: 1', '  b: 2'])).toBe(0);
  });

  it('is zero when there is no content at all', () => {
    expect(topIndent(['', '# 只有注释', '   '])).toBe(0);
  });

  it('is zero for an empty file', () => {
    expect(topIndent([])).toBe(0);
  });
});

describe('isContent', () => {
  it.each(['a: 1', '  - x', '  a: 1', 'a: 1  # 行尾注释', '-'])('reads %s as content', (line) => {
    expect(isContent(line)).toBe(true);
  });

  it.each(['', '   ', '\t', '# 说明', '  # 说明', '#紧挨着的注释'])('does not read %s as content', (line) => {
    expect(isContent(line)).toBe(false);
  });
});

describe('isSequenceItem', () => {
  it.each(['- x', '   - x', '-', '  -', '- '])('reads %s as a sequence item', (line) => {
    expect(isSequenceItem(line)).toBe(true);
  });

  it.each(['-x', '--', '', 'a: 1', 'a - b'])('does not read %s as a sequence item', (line) => {
    expect(isSequenceItem(line)).toBe(false);
  });
});

describe('isBlockScalarHead', () => {
  it.each(['|', '>', '|-', '>-', '|2+', '>+'])('reads %s as a block scalar head', (head) => {
    expect(isBlockScalarHead(head)).toBe(true);
  });

  it.each(['', 'gpt-4o', '|abc', '||', '"|"'])('does not read %s as a block scalar head', (value) => {
    expect(isBlockScalarHead(value)).toBe(false);
  });
});

describe('commentAfter', () => {
  it('returns the comment with the whitespace in front of it', () => {
    expect(commentAfter(' json   # 行尾注释')).toBe('   # 行尾注释');
  });

  it('ignores a # inside double quotes', () => {
    expect(commentAfter(' "a # b"')).toBe('');
  });

  it('needs whitespace before the #', () => {
    expect(commentAfter(' a#b')).toBe('');
  });

  it('gives up on an apostrophe in a plain scalar', () => {
    // 认不准引号范围时宁可丢一句注释，也不能把取值错切成注释。
    expect(commentAfter(" it's # x")).toBe('');
  });

  it('sees past an escaped quote', () => {
    expect(commentAfter(' "a\\" # b" # 真注释')).toBe(' # 真注释');
  });

  it('sees past a doubled single quote', () => {
    expect(commentAfter(" 'it''s' # c")).toBe(' # c');
  });

  it('takes a comment that starts right at the beginning', () => {
    expect(commentAfter('# 只有注释')).toBe('# 只有注释');
  });

  it('returns empty for a value with no comment', () => {
    expect(commentAfter(' gpt-4o')).toBe('');
  });
});

describe('openBlock', () => {
  it.each([...EMPTY_PLACEHOLDERS])('turns %s into a block key and keeps the comment', (placeholder) => {
    const lines = [`  options: ${placeholder}  # 说明`];

    openBlock(lines, locate(lines, ['options']), ['providers', 'options'], ['reasoning_effort']);

    expect(lines[0]).toBe('  options:  # 说明');
  });

  it('keeps a quoted key quoted while opening it', () => {
    const lines = ['"2024": {}  # 说明'];

    openBlock(lines, locate(lines, ['2024']), ['2024'], ['x']);

    expect(lines[0]).toBe('"2024":  # 说明');
  });

  it('leaves a key that is already a block alone', () => {
    const lines = ['a:'];

    openBlock(lines, locate(lines, ['a']), ['a'], ['b']);

    expect(lines[0]).toBe('a:');
  });

  it('refuses a flow mapping instead of guessing', () => {
    const lines = ['a: {b: 1}'];
    const boom = (): void => openBlock(lines, locate(lines, ['a']), ['x', 'a'], ['c', 'd']);

    expect(boom).toThrow(YamlShapeError);
    expect(boom).toThrowError(new YamlShapeError('x.a 已经有值 {b: 1}，无法在它下面加 c.d'));
  });

  it('refuses a scalar value', () => {
    const lines = ['model: gpt-4o  # 说明'];
    const boom = (): void => openBlock(lines, locate(lines, ['model']), ['model'], ['sub']);

    expect(boom).toThrowError(new YamlShapeError('model 已经有值 gpt-4o，无法在它下面加 sub'));
  });
});

describe('EMPTY_PLACEHOLDERS', () => {
  it('holds exactly the four empty spellings', () => {
    expect([...EMPTY_PLACEHOLDERS].sort()).toEqual(['null', '[]', '{}', '~'].sort());
  });
});

describe('splitLines', () => {
  it('keeps CRLF as the line ending and leaves no \\r on the lines', () => {
    // 按 '\n' 硬拆会让每行尾巴留一个 \r，键名就此匹配不上。
    expect(splitLines('a: 1\r\nb: 2\r\n')).toEqual({ lines: ['a: 1', 'b: 2', ''], eol: '\r\n' });
  });

  it('keeps LF', () => {
    expect(splitLines('a: 1\nb: 2\n')).toEqual({ lines: ['a: 1', 'b: 2', ''], eol: '\n' });
  });

  it('defaults to LF for a single line', () => {
    expect(splitLines('a: 1')).toEqual({ lines: ['a: 1'], eol: '\n' });
  });
});

describe('indentOf', () => {
  it('counts the leading spaces', () => {
    expect(indentOf('    - x')).toBe(4);
  });

  it('is zero at the left margin', () => {
    expect(indentOf('a: 1')).toBe(0);
  });
});
