import { describe, expect, it } from 'vitest';

import yaml from 'js-yaml';

import { NodeYamlShapeError, readYamlScalar, upsertYamlNested } from '../console/nodeYaml';

// 仿 config/runtime.yaml 的形状：注释在顶部、行尾和块之间都有。
const RUNTIME = [
  '# 顶部注释',
  'engine:',
  '  tool_mode: json                   # 行尾注释',
  '',
  'providers:',
  '  openai:',
  '    timeout_sec: 600.0',
  '',
  '# 尾部注释',
  'routing:',
  '  vision:',
  '',
].join('\n');

describe('upsertYamlNested', () => {
  it('在已有块下建出缺失层级，注释一行不动', () => {
    const next = upsertYamlNested(RUNTIME, ['providers', 'openai', 'options', 'reasoning_effort'], 'high');
    expect(next).toContain('# 顶部注释');
    expect(next).toContain('  tool_mode: json                   # 行尾注释');
    expect(next).toContain('# 尾部注释');
    expect(readYamlScalar(next, ['providers', 'openai', 'options', 'reasoning_effort'])).toBe('high');
    expect(readYamlScalar(next, ['providers', 'openai', 'timeout_sec'])).toBe('600.0');
  });

  it('改已存在的键不会重复插入', () => {
    const once = upsertYamlNested(RUNTIME, ['providers', 'openai', 'options', 'verbosity'], 'low');
    const twice = upsertYamlNested(once, ['providers', 'openai', 'options', 'verbosity'], 'high');
    expect(twice.match(/verbosity:/g)).toHaveLength(1);
    expect(readYamlScalar(twice, ['providers', 'openai', 'options', 'verbosity'])).toBe('high');
  });

  it('空值删掉整行', () => {
    const added = upsertYamlNested(RUNTIME, ['providers', 'openai', 'options', 'verbosity'], 'low');
    const removed = upsertYamlNested(added, ['providers', 'openai', 'options', 'verbosity'], '');
    expect(removed).not.toContain('verbosity');
  });

  it('整条路径都不存在时也能落地', () => {
    const next = upsertYamlNested(RUNTIME, ['providers', 'gemini', 'options', 'thinking_mode'], 'level');
    expect(readYamlScalar(next, ['providers', 'gemini', 'options', 'thinking_mode'])).toBe('level');
    expect(readYamlScalar(next, ['providers', 'openai', 'timeout_sec'])).toBe('600.0');
  });

  it('不会把值写进同名的隔壁块', () => {
    const next = upsertYamlNested(RUNTIME, ['providers', 'openai', 'options', 'top_p'], '0.9');
    expect(readYamlScalar(next, ['routing', 'vision'])).toBe('');
    expect(next.indexOf('top_p')).toBeLessThan(next.indexOf('# 尾部注释'));
  });

  it('把 options: {} 占位换成块写法，而不是产出坏 yaml', () => {
    const withPlaceholder = RUNTIME.replace(
      '    timeout_sec: 600.0',
      '    timeout_sec: 600.0\n    options: {}',
    );
    const next = upsertYamlNested(withPlaceholder, ['providers', 'openai', 'options', 'verbosity'], 'low');
    expect(next).not.toContain('options: {}');
    expect(readYamlScalar(next, ['providers', 'openai', 'options', 'verbosity'])).toBe('low');
    expect(yaml.load(next)).toMatchObject({
      providers: { openai: { timeout_sec: 600, options: { verbosity: 'low' } } },
    });
  });

  it('父键已有真值时拒绝写入，不赌一把改坏文件', () => {
    const conflicting = RUNTIME.replace('    timeout_sec: 600.0', '    options: 5');
    expect(() => upsertYamlNested(conflicting, ['providers', 'openai', 'options', 'verbosity'], 'low'))
      .toThrow(NodeYamlShapeError);
  });

  it('产出的始终是合法 yaml', () => {
    let next = RUNTIME;
    next = upsertYamlNested(next, ['providers', 'openai', 'options', 'reasoning_effort'], 'high');
    next = upsertYamlNested(next, ['providers', 'openai', 'options', 'temperature'], '0.7');
    next = upsertYamlNested(next, ['providers', 'gemini', 'options', 'thinking_mode'], 'level');
    expect(yaml.load(next)).toMatchObject({
      engine: { tool_mode: 'json' },
      providers: {
        openai: { timeout_sec: 600, options: { reasoning_effort: 'high', temperature: 0.7 } },
        gemini: { options: { thinking_mode: 'level' } },
      },
    });
  });

  it('CRLF 原样保留', () => {
    const crlf = RUNTIME.split('\n').join('\r\n');
    const next = upsertYamlNested(crlf, ['providers', 'openai', 'options', 'top_p'], '0.9');
    expect(next).not.toMatch(/[^\r]\n/);
  });
});
