// 节点 yaml 的定点编辑。这个文件里有两百行 block scalar 的 prompt，
// 改错一次就把整个节点毁了，所以每条路径都要钉住。
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import yaml from 'js-yaml';
import { describe, expect, it } from 'vitest';

import {
  NodeYamlShapeError,
  readYamlList,
  readYamlScalar,
  replaceYamlList,
  replaceYamlScalar,
  upsertYamlList,
  upsertYamlNested,
  upsertYamlScalar,
  yamlString,
} from '../console/nodeYaml';

// 贴着 defaults/nodes/qq.orchestrator.yaml 的真实形状：顶层列表、嵌套列表、末尾 block scalar。
const NODE = [
  'id: qq.orchestrator',
  'type: ai',
  'name: "QQ 综合入口"',
  'delegate_targets:',
  '  - bootstrap.executor',
  '  - draw.image_gen',
  'tool_access:',
  '  mode: allowlist',
  '  allow:',
  '    - web_search',
  '    - read_web',
  'skills:',
  '  mode: none',
  'prompt: |',
  '  {{include:_persona.md}}',
  '',
  '  # Role: QQ Orchestrator',
  '  - 这一行以短横开头，但它是 prompt 正文',
  '  tool_access:',
  '    allow:',
  '      - 这是 prompt 里的假配置',
  '',
].join('\n');

// 左边是用户输进来的文字，右边是它该有的 yaml 写法。
const SPELLINGS: [string, string][] = [
  ['gpt-4o', 'gpt-4o'],
  ['你好世界', '你好世界'],
  ['a-b_c.d', 'a-b_c.d'],
  ["it's", "it's"],
  ['a # b', '"a # b"'],
  ['12:30', '"12:30"'],
  ['123', '"123"'],
  ['1.5', '"1.5"'],
  ['true', '"true"'],
  ['yes', '"yes"'],
  ['no', '"no"'],
  ['on', '"on"'],
  ['off', '"off"'],
  ['null', '"null"'],
  ['~', '"~"'],
  ['', '""'],
  ['  留白  ', '"  留白  "'],
  ['- 开头是短横', '"- 开头是短横"'],
  ['[a, b]', '"[a, b]"'],
];

describe('yamlString', () => {
  it.each(SPELLINGS)('writes %j as %j', (text, written) => {
    expect(yamlString(text)).toBe(written);
  });

  it('writes multi-line text as one escaped line', () => {
    // 写成块标量就得管缩进，塞回 `key: ` 后面只能用转义。
    expect(yamlString('第一行\n第二行')).toBe('"第一行\\n第二行"');
  });

  it('escapes quotes and backslashes it cannot leave bare', () => {
    expect(yamlString('说 "你好"\n再见\\了')).toBe('"说 \\"你好\\"\\n再见\\\\了"');
  });

  it.each([...SPELLINGS.map(([text]) => text), '第一行\n第二行', '说 "你好"', 'C:\\dir\\file'])(
    'reads %j back as the same string',
    (text) => {
      expect(yaml.load(`k: ${yamlString(text)}`)).toEqual({ k: text });
    },
  );
});

describe('readYamlList', () => {
  it('reads a nested list', () => {
    expect(readYamlList(NODE, ['tool_access', 'allow'])).toEqual(['web_search', 'read_web']);
  });

  it('reads a top-level list', () => {
    expect(readYamlList(NODE, ['delegate_targets'])).toEqual(['bootstrap.executor', 'draw.image_gen']);
  });

  it('reads a list written flush with its key', () => {
    // PyYAML 默认不给列表项多缩一层，工具写出来的 yaml 都长这样。
    expect(readYamlList('presets:\n- a\n- b\nnext: 1\n', ['presets'])).toEqual(['a', 'b']);
  });

  it('returns empty for a key that is not there', () => {
    expect(readYamlList(NODE, ['nope'])).toEqual([]);
  });

  it('does not reach into the prompt body for a same-named key', () => {
    // prompt 里也有 tool_access.allow，认错了就会把用户的提示词当配置改。
    expect(readYamlList(NODE, ['tool_access', 'allow'])).not.toContain('这是 prompt 里的假配置');
  });

  it('stops at the parent block instead of borrowing a key from further down', () => {
    // 节点走 denylist、根本没配 allow 时，不能一路搜到 prompt 里去捞一个。
    const noAllow = [
      'tool_access:',
      '  mode: denylist',
      'prompt: |',
      '  tool_access:',
      '    allow:',
      '      - 这是 prompt 里的假配置',
      '',
    ].join('\n');

    expect(readYamlList(noAllow, ['tool_access', 'allow'])).toEqual([]);
  });
});

describe('replaceYamlList', () => {
  it('swaps the entries and leaves everything else byte for byte', () => {
    const out = replaceYamlList(NODE, ['tool_access', 'allow'], ['web_search', 'qq_forward']);

    expect(readYamlList(out, ['tool_access', 'allow'])).toEqual(['web_search', 'qq_forward']);
    expect(out).toContain('name: "QQ 综合入口"');
    expect(out).toContain('  mode: allowlist');
    expect(out).toContain('prompt: |');
    expect(out).toContain('  - 这一行以短横开头，但它是 prompt 正文');
    expect(out).toContain('      - 这是 prompt 里的假配置');
  });

  it('keeps the prompt block scalar intact', () => {
    // js-yaml round-trip 会把它压成带引号的长字符串，那等于毁掉这个节点。
    const out = replaceYamlList(NODE, ['delegate_targets'], ['bootstrap.executor']);

    expect(out.slice(out.indexOf('prompt: |'))).toBe(NODE.slice(NODE.indexOf('prompt: |')));
  });

  it('writes an empty list as a key with no entries', () => {
    const out = replaceYamlList(NODE, ['tool_access', 'allow'], []);

    expect(out).toContain('  allow:\nskills:');
    expect(readYamlList(out, ['tool_access', 'allow'])).toEqual([]);
  });

  it('keeps the nesting depth of the original list', () => {
    const out = replaceYamlList(NODE, ['tool_access', 'allow'], ['x']);

    expect(out).toContain('    - x');
  });

  it('leaves a flush list flush', () => {
    expect(replaceYamlList('presets:\n- a\n- b\nnext: 1\n', ['presets'], ['x', 'y']))
      .toBe('presets:\n- x\n- y\nnext: 1\n');
  });

  it('indents by two when there is no entry to copy the indent from', () => {
    expect(replaceYamlList('allow:\nnext: 1\n', ['allow'], ['x'])).toBe('allow:\n  - x\nnext: 1\n');
  });

  it('refuses a flow-style list instead of mangling it', () => {
    const flow = 'tool_access:\n  allow: [web_search, read_web]\n';

    expect(() => replaceYamlList(flow, ['tool_access', 'allow'], ['x'])).toThrow(NodeYamlShapeError);
  });

  it('refuses when the block holds something other than list items', () => {
    const odd = 'tool_access:\n  allow:\n    weird: true\n';

    expect(() => replaceYamlList(odd, ['tool_access', 'allow'], ['x'])).toThrow(NodeYamlShapeError);
  });

  it('refuses a key it cannot find', () => {
    expect(() => replaceYamlList(NODE, ['ghost'], ['x'])).toThrow(NodeYamlShapeError);
  });

  it('does not eat the blank line that separates blocks', () => {
    const spaced = 'a:\n  - one\n\nb: 2\n';

    expect(replaceYamlList(spaced, ['a'], ['one', 'two'])).toBe('a:\n  - one\n  - two\n\nb: 2\n');
  });

  it('survives a round trip through itself', () => {
    const once = replaceYamlList(NODE, ['tool_access', 'allow'], ['web_search', 'read_web']);

    expect(once).toBe(NODE);
  });
});

describe('upsertYamlScalar', () => {
  it('inserts a key that was never there', () => {
    // qq.orchestrator 没写 model，要设就得插一行进去。
    const out = upsertYamlScalar(NODE, 'model', 'gpt-4o', 'type');

    expect(out).toContain('type: ai\nmodel: gpt-4o\n');
    expect(readYamlScalar(out, ['model'])).toBe('gpt-4o');
  });

  it('replaces the key when it is already there', () => {
    const once = upsertYamlScalar(NODE, 'model', 'gpt-4o', 'type');

    const twice = upsertYamlScalar(once, 'model', 'claude', 'type');

    expect(readYamlScalar(twice, ['model'])).toBe('claude');
    expect(twice.split('\n').length).toBe(once.split('\n').length);
  });

  it('removes the line when cleared, restoring the original', () => {
    const added = upsertYamlScalar(NODE, 'model', 'gpt-4o', 'type');

    expect(upsertYamlScalar(added, 'model', '', 'type')).toBe(NODE);
  });

  it('does nothing when clearing a key that was never there', () => {
    expect(upsertYamlScalar(NODE, 'model', '', 'type')).toBe(NODE);
  });

  it('does not mistake a nested key for the top-level one', () => {
    // tool_access.mode 缩进两格，不能被当成顶层 mode。
    const out = upsertYamlScalar(NODE, 'mode', 'fast', 'type');

    expect(out).toContain('type: ai\nmode: fast\n');
    expect(out).toContain('  mode: allowlist');
  });

  it('inserts after the whole anchor block, not into the middle of it', () => {
    // 插在 `prompt:` 那一行后面就把正文切成两半，节点当场废掉。
    expect(upsertYamlScalar('id: x\nprompt: |\n  一\n  二\ntype: ai\n', 'model', 'gpt-4o', 'prompt'))
      .toBe('id: x\nprompt: |\n  一\n  二\nmodel: gpt-4o\ntype: ai\n');
  });

  it('inserts after the last item when the anchor carries a list', () => {
    expect(upsertYamlScalar('delegate_targets:\n  - a\n  - b\nn: 1\n', 'model', 'gpt-4o', 'delegate_targets'))
      .toBe('delegate_targets:\n  - a\n  - b\nmodel: gpt-4o\nn: 1\n');
  });

  it('lines the new key up with the anchor', () => {
    expect(upsertYamlScalar('  a: 1\n  b: 2\n', 'c', '3', 'a')).toBe('  a: 1\n  c: 3\n  b: 2\n');
  });

  it('recognises a key that the file spells with quotes', () => {
    // 认不出来就会在下面再插一个 model，同一个键写两遍。
    expect(upsertYamlScalar('"model": old\nid: x\n', 'model', 'new', 'id')).toBe('"model": new\nid: x\n');
  });

  it('refuses when the anchor is missing', () => {
    expect(() => upsertYamlScalar(NODE, 'model', 'x', 'nonexistent')).toThrow(NodeYamlShapeError);
  });
});

describe('against the real qq.orchestrator.yaml', () => {
  // 手写的样例再像也只是样例。真文件格式一变，这里先红。
  const NODE_PATH = resolve(
    dirname(fileURLToPath(import.meta.url)),
    '../../../../../defaults/nodes/qq.orchestrator.yaml',
  );
  const real = readFileSync(NODE_PATH, 'utf-8');

  it('reads the tools that are actually configured', () => {
    const tools = readYamlList(real, ['tool_access', 'allow']);

    expect(tools).toContain('web_search');
    expect(tools).toContain('qq_forward');
    expect(tools.every((name) => /^[a-z0-9_]+$/.test(name))).toBe(true);
  });

  it('reads the delegate targets', () => {
    expect(readYamlList(real, ['delegate_targets'])).toContain('draw.image_gen');
  });

  it('rewriting the same values gives back the identical file', () => {
    const tools = readYamlList(real, ['tool_access', 'allow']);
    const targets = readYamlList(real, ['delegate_targets']);

    let out = replaceYamlList(real, ['tool_access', 'allow'], tools);
    out = replaceYamlList(out, ['delegate_targets'], targets);

    expect(out).toBe(real);
  });

  it('dropping one tool touches nothing but that line', () => {
    const tools = readYamlList(real, ['tool_access', 'allow']);

    const out = replaceYamlList(real, ['tool_access', 'allow'], tools.filter((t) => t !== 'read_web'));

    expect(out.split('\n').length).toBe(real.split('\n').length - 1);
    expect(out).toContain('{{include:_persona.md}}');
    expect(out.slice(out.indexOf('prompt: |'))).toBe(real.slice(real.indexOf('prompt: |')));
  });
});

describe('scalars', () => {
  it('reads a quoted value without the quotes', () => {
    expect(readYamlScalar(NODE, ['name'])).toBe('QQ 综合入口');
  });

  it('reads a nested value', () => {
    expect(readYamlScalar(NODE, ['tool_access', 'mode'])).toBe('allowlist');
  });

  it('returns empty for a missing key', () => {
    expect(readYamlScalar(NODE, ['model'])).toBe('');
  });

  it('replaces a value in place', () => {
    const out = replaceYamlScalar(NODE, ['type'], 'tool');

    expect(out).toContain('type: tool');
    expect(out).not.toContain('type: ai');
  });

  it('deletes the line when the value is cleared', () => {
    // 删掉 model 这一行 = 回落节点默认，和写空串是两种意思。
    const out = replaceYamlScalar('id: x\nmodel: gpt-4o\ntype: ai\n', ['model'], '');

    expect(out).toBe('id: x\ntype: ai\n');
  });

  it('is a no-op when clearing a key that was never there', () => {
    expect(replaceYamlScalar(NODE, ['model'], '')).toBe(NODE);
  });

  it('refuses to overwrite a block with a scalar', () => {
    expect(() => replaceYamlScalar(NODE, ['tool_access'], 'nope')).toThrow(NodeYamlShapeError);
  });

  it('keeps indentation when replacing a nested value', () => {
    const out = replaceYamlScalar(NODE, ['tool_access', 'mode'], 'denylist');

    expect(out).toContain('  mode: denylist');
    expect(out).toContain('skills:\n  mode: none');
  });

  it('reads a value without its trailing comment', () => {
    expect(readYamlScalar('model: gpt-4o  # 主模型\n', ['model'])).toBe('gpt-4o');
  });

  it('reads a quoted value that has a comment behind it', () => {
    expect(readYamlScalar('name: "x"  # 说明\n', ['name'])).toBe('x');
  });

  it('keeps the trailing comment when replacing a value', () => {
    // 冲掉注释等于把这一行的说明吃了，改一次值少一句文档。
    expect(replaceYamlScalar('model: gpt-4o  # 主模型\nn: 1\n', ['model'], 'claude'))
      .toBe('model: claude  # 主模型\nn: 1\n');
  });

  it('keeps the comment that follows a quoted value', () => {
    expect(replaceYamlScalar('key: "a" # c\n', ['key'], '"b"')).toBe('key: "b" # c\n');
  });

  it('unescapes a double-quoted value', () => {
    expect(readYamlScalar('name: "a\\nb"\n', ['name'])).toBe('a\nb');
  });

  it('unescapes a quote inside a double-quoted value', () => {
    expect(readYamlScalar('key: "it\\"s"\n', ['key'])).toBe('it"s');
  });

  it('unescapes a doubled quote inside a single-quoted value', () => {
    expect(readYamlScalar("key: 'it''s'\n", ['key'])).toBe("it's");
  });

  it('gives a bare value back exactly as it is written', () => {
    // 裸写的数字原样显示，不然 007 会变成 7。
    expect(readYamlScalar('n: 007\n', ['n'])).toBe('007');
  });

  it('reads a value under a quoted key', () => {
    expect(readYamlScalar('"2024": jan\n', ['2024'])).toBe('jan');
  });

  it('does not rename a quoted key while replacing its value', () => {
    // 写回成 `2024:` 的话，这个键下次就读成数字了。
    expect(replaceYamlScalar('"2024": 1\nn: 2\n', ['2024'], '9')).toBe('"2024": 9\nn: 2\n');
  });

  it('does not unquote a single-quoted key', () => {
    expect(replaceYamlScalar("'2024': 1\nn: 2\n", ['2024'], '9')).toBe("'2024': 9\nn: 2\n");
  });

  it('keeps a colon that belongs to the key', () => {
    // 按第一个冒号切的话，这一行会被写成 `a: 9`，键名当场丢了半截。
    expect(replaceYamlScalar('a:b: 1\nn: 2\n', ['a:b'], '9')).toBe('a:b: 9\nn: 2\n');
  });

  it('keeps the comment behind a quoted key that holds a colon', () => {
    expect(replaceYamlScalar('"x:y": 1  # c\n', ['x:y'], '9')).toBe('"x:y": 9  # c\n');
  });

  it('deletes the line of a quoted key', () => {
    expect(replaceYamlScalar('"2024": 1\nn: 2\n', ['2024'], '')).toBe('n: 2\n');
  });
});

describe('upsertYamlNested', () => {
  it('does not take a trailing comment for a value on the parent', () => {
    // `engine:  # 说明` 是块写法，冒号后面只有注释；当成「已经有值」就再也写不进去了。
    expect(upsertYamlNested('engine:  # 说明\n', ['engine', 'tool_mode'], 'json'))
      .toBe('engine:  # 说明\n  tool_mode: json\n');
  });

  it('opens a {} placeholder into a block and keeps the comment', () => {
    const raw = 'providers:\n  openai:\n    options: {}  # 注释\n';

    expect(upsertYamlNested(raw, ['providers', 'openai', 'options', 'reasoning_effort'], 'high'))
      .toBe('providers:\n  openai:\n    options:  # 注释\n      reasoning_effort: high\n');
  });
});

describe('upsertYamlList', () => {
  it('does not take a trailing comment for a value on the parent', () => {
    expect(upsertYamlList('tool_access:  # 说明\n  mode: all\n', ['tool_access', 'allow'], ['a', 'b']))
      .toBe('tool_access:  # 说明\n  mode: all\n  allow:\n    - a\n    - b\n');
  });

  it('opens a {} placeholder into a block and keeps the comment', () => {
    expect(upsertYamlList('tool_access:\n  extra: {}  # 注释\n', ['tool_access', 'extra', 'allow'], ['a']))
      .toBe('tool_access:\n  extra:  # 注释\n    allow:\n      - a\n');
  });

  it('refuses when the parent really does have a value', () => {
    expect(() => upsertYamlList('tool_access:\n  extra: 5\n', ['tool_access', 'extra', 'allow'], ['a']))
      .toThrow(NodeYamlShapeError);
  });
});
