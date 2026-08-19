// 节点 yaml 的定点编辑。这个文件里有两百行 block scalar 的 prompt，
// 改错一次就把整个节点毁了，所以每条路径都要钉住。
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import {
  NodeYamlShapeError,
  readYamlList,
  readYamlScalar,
  replaceYamlList,
  replaceYamlScalar,
  upsertYamlScalar,
} from '../console/nodeYaml';

// 贴着 config/nodes/qq.orchestrator.yaml 的真实形状：顶层列表、嵌套列表、末尾 block scalar。
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

describe('readYamlList', () => {
  it('reads a nested list', () => {
    expect(readYamlList(NODE, ['tool_access', 'allow'])).toEqual(['web_search', 'read_web']);
  });

  it('reads a top-level list', () => {
    expect(readYamlList(NODE, ['delegate_targets'])).toEqual(['bootstrap.executor', 'draw.image_gen']);
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

  it('refuses when the anchor is missing', () => {
    expect(() => upsertYamlScalar(NODE, 'model', 'x', 'nonexistent')).toThrow(NodeYamlShapeError);
  });
});

describe('against the real qq.orchestrator.yaml', () => {
  // 手写的样例再像也只是样例。真文件格式一变，这里先红。
  const NODE_PATH = resolve(
    dirname(fileURLToPath(import.meta.url)),
    '../../../../../config/nodes/qq.orchestrator.yaml',
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
});
