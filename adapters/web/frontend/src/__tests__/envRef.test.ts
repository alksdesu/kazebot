// 与后端 clonoth_runtime.resolve_env_ref 对齐的识别规则。
import { describe, expect, it } from 'vitest';

import { describeEnvRef, envRefNames } from '../utils/envRef';

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

  it('空模板和没闭合的都当字面量，跟后端一个口径', () => {
    expect(envRefNames('${}')).toEqual([]);
    expect(envRefNames('$ENV{}')).toEqual([]);
    expect(envRefNames('$ENV{BROKEN')).toEqual([]);
  });

  it('前后有空白也认', () => {
    expect(envRefNames('  ${OPENAI_MODEL}  ')).toEqual(['OPENAI_MODEL']);
    expect(envRefNames('$ENV{ A | B }')).toEqual(['A', 'B']);
  });
});

describe('describeEnvRef', () => {
  it('引用换成人能读的变量名', () => {
    expect(describeEnvRef('$ENV{QQ_MAIN_MODEL}')).toBe('环境变量 QQ_MAIN_MODEL');
    expect(describeEnvRef('$ENV{A|B}')).toBe('环境变量 A 或 B');
  });

  it('不是引用就原样过', () => {
    expect(describeEnvRef('gpt-4o-mini')).toBe('gpt-4o-mini');
    expect(describeEnvRef('')).toBe('');
  });
});
