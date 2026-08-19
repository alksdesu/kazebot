// 信号名的展示映射。后端加了新信号时界面必须退化成英文标识，不能漏掉整行判定。
import { describe, expect, it } from 'vitest';

import { blockerLabel, showSignalTag, signalLabel } from '../console/signals';

describe('signalLabel', () => {
  it('names every signal the policy can report', () => {
    const known = ['all', 'at', 'reply', 'name', 'prefix', 'keyword', 'random', 'llm_intent'];

    expect(known.every((name) => signalLabel(name) !== name)).toBe(true);
  });

  it('falls back to the raw identifier for an unknown signal', () => {
    expect(signalLabel('teleport')).toBe('teleport');
  });

  it('reads as 无 when there is no signal at all', () => {
    expect(signalLabel('')).toBe('无');
  });
});

describe('blockerLabel', () => {
  it('names the cooldown blocker', () => {
    expect(blockerLabel('cooldown')).toBe('冷却');
  });
});

describe('showSignalTag', () => {
  it('drops the tag when the reason already says it', () => {
    // 否则读起来是「会回应 被 @ 被 @」。
    expect(showSignalTag('at', '被 @')).toBe(false);
    expect(showSignalTag('name', '出现名字「咪啪」')).toBe(false);
    expect(showSignalTag('reply', '命中被回复，但本群的冷却还剩 29.0s')).toBe(false);
  });

  it('keeps the tag when the reason words it differently', () => {
    // 「命中前缀「!」」没说这是哪条规则开的，标签补上这个分类。
    expect(showSignalTag('prefix', '命中前缀「!」')).toBe(true);
    expect(showSignalTag('keyword', '命中关键词「下雨」')).toBe(true);
    expect(showSignalTag('all', '全量模式：群内任意消息都回')).toBe(true);
  });

  it('drops the tag for the model handover too', () => {
    // 「交给模型判断要不要接话」里已经有「模型判断」四个字。
    expect(showSignalTag('llm_intent', '其他规则都不命中，交给模型判断要不要接话')).toBe(false);
  });

  it('shows nothing when no signal matched', () => {
    expect(showSignalTag('', '没有命中任何开着的信号')).toBe(false);
  });
});
