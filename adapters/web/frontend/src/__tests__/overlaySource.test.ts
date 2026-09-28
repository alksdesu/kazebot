// 覆盖层的来源标签和删除说法：节点、节点文件、工具三处共用这一套，说法错了用户就会删错东西。
import { describe, expect, it } from 'vitest';

import { overlaySourceLabel, removalFor, toolOriginLabel } from '../utils/overlaySource';

describe('overlaySourceLabel', () => {
  it('四种来源各有各的说法', () => {
    expect(overlaySourceLabel('system')).toBe('系统');
    expect(overlaySourceLabel('default')).toBe('仓库默认');
    expect(overlaySourceLabel('override')).toBe('改过');
    expect(overlaySourceLabel('custom')).toBe('自建');
  });

  it('改过却和默认一字不差的单独标出来', () => {
    expect(overlaySourceLabel('override', true)).toBe('改过 · 与默认相同');
    expect(overlaySourceLabel('override', false)).toBe('改过');
  });

  it('只有 override 在意与默认相同，别的来源不受影响', () => {
    expect(overlaySourceLabel('default', true)).toBe('仓库默认');
    expect(overlaySourceLabel('custom', true)).toBe('自建');
    expect(overlaySourceLabel('system', true)).toBe('系统');
  });

  it('老后端不给来源，标签就空着不占位', () => {
    expect(overlaySourceLabel(undefined)).toBe('');
    expect(overlaySourceLabel(undefined, true)).toBe('');
  });
});

describe('toolOriginLabel', () => {
  it('三种来源各有各的说法', () => {
    expect(toolOriginLabel('bundled')).toBe('仓库自带');
    expect(toolOriginLabel('override')).toBe('改过');
    expect(toolOriginLabel('custom')).toBe('自建');
  });

  it('老后端不给来源，标签就空着不占位', () => {
    expect(toolOriginLabel(undefined)).toBe('');
  });
});

describe('removalFor', () => {
  it('改过的仓库默认是恢复默认，东西还在', () => {
    const action = removalFor('override', '节点 chat.intent');
    expect(action).toEqual({
      label: '恢复默认',
      confirm: '把节点 chat.intent恢复成仓库默认？本实例对它的修改会删掉。',
      done: '节点 chat.intent已恢复成仓库默认',
      restores: true,
    });
  });

  it('自己加的是删除，东西没了', () => {
    const action = removalFor('custom', '工具 demo_tool');
    expect(action).toEqual({
      label: '删除',
      confirm: '确定删除工具 demo_tool吗？对应的文件会被删掉。',
      done: '工具 demo_tool已删除',
      restores: false,
    });
  });

  it('没改过的仓库默认和系统节点没有本实例的文件可删', () => {
    expect(removalFor('default', '节点 chat.intent')).toBeNull();
    expect(removalFor('system', '节点 system.compactor')).toBeNull();
    expect(removalFor('bundled', '工具 exa_search')).toBeNull();
  });

  it('老后端不给来源就判断不了，交给调用方自己兜底', () => {
    expect(removalFor(undefined, '节点 chat.intent')).toBeNull();
  });

  it('说法里带上是谁，一句话看得出删的是哪个', () => {
    expect(removalFor('custom', '文件 a.yaml')?.confirm).toContain('文件 a.yaml');
    expect(removalFor('override', '文件 a.yaml')?.done).toContain('文件 a.yaml');
  });
});
