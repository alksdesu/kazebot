// 与后端 clonoth_runtime.resolve_env_ref 对齐的识别规则。
import { describe, expect, it } from 'vitest';

import { describeEnvRef, envRefNames, parseEnvRef } from '../utils/envRef';

describe('envRefNames', () => {
  it('两种写法都认', () => {
    expect(envRefNames('$ENV{QQ_MAIN_MODEL}')).toEqual(['QQ_MAIN_MODEL']);
    expect(envRefNames('${OPENAI_MODEL}')).toEqual(['OPENAI_MODEL']);
  });

  it('摊开 | 分隔的回退列表', () => {
    // config/nodes/draw.image_gen.yaml 里就是这个写法。
    expect(envRefNames('$ENV{DRAW_TAG_MODEL|DRAW_PLANNER_MODEL}')).toEqual(['DRAW_TAG_MODEL', 'DRAW_PLANNER_MODEL']);
  });

  it('字面量模型名不是引用', () => {
    expect(envRefNames('gpt-4o-mini')).toEqual([]);
    expect(envRefNames('relay/qwen3-max')).toEqual([]);
    expect(envRefNames('')).toEqual([]);
  });

  it('前后有空白也认', () => {
    expect(envRefNames('  ${OPENAI_MODEL}  ')).toEqual(['OPENAI_MODEL']);
    expect(envRefNames('$ENV{ A | B }')).toEqual(['A', 'B']);
  });
});

describe('parseEnvRef', () => {
  // runtime 一直把 $ENV{} 当引用展开，结果是空串；${} 要求括号里有东西，空的才原样当字面量。
  it('$ENV{} 是一个变量名都没写的引用，${} 不是引用', () => {
    expect(parseEnvRef('$ENV{}')).toEqual([]);
    expect(parseEnvRef('$ENV{ | }')).toEqual([]);
    expect(parseEnvRef('${}')).toBeNull();
  });

  it('没闭合的、字面量都不是引用', () => {
    expect(parseEnvRef('$ENV{BROKEN')).toBeNull();
    expect(parseEnvRef('${BROKEN')).toBeNull();
    expect(parseEnvRef('gpt-4o-mini')).toBeNull();
    expect(parseEnvRef('')).toBeNull();
  });

  it('写了变量名的引用给出变量名', () => {
    expect(parseEnvRef('$ENV{NEW|OLD}')).toEqual(['NEW', 'OLD']);
    expect(parseEnvRef('${OPENAI_MODEL}')).toEqual(['OPENAI_MODEL']);
  });
});

describe('describeEnvRef', () => {
  it('引用换成人能读的变量名', () => {
    expect(describeEnvRef('$ENV{QQ_MAIN_MODEL}')).toBe('环境变量 QQ_MAIN_MODEL');
    expect(describeEnvRef('$ENV{A|B}')).toBe('环境变量 A 或 B');
  });

  it('不是引用就原样过', () => {
    expect(describeEnvRef('gpt-4o-mini')).toBe('gpt-4o-mini');
    expect(describeEnvRef('${}')).toBe('${}');
    expect(describeEnvRef('')).toBe('');
  });

  it('$ENV{} 展开是空串，交给调用方说没设置', () => {
    expect(describeEnvRef('$ENV{}')).toBe('');
  });
});
