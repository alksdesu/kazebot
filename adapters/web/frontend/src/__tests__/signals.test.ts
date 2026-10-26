// 信号名的展示映射。后端加了新信号时界面必须退化成英文标识，不能漏掉整行判定。
import { describe, expect, it } from 'vitest';

import type { PlatformDryRunPresence, PlatformDryRunVerdict } from '../api/supervisorClient';
import {
  blockerLabel, presenceSummary, secondsText, showSignalTag, signalLabel, verdictStatus,
} from '../console/signals';

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

  it('names the presence gates', () => {
    expect(blockerLabel('gate')).toBe('概率门');
    expect(blockerLabel('density')).toBe('主动回复上限');
  });
});

describe('showSignalTag', () => {
  it('drops the tag when the reason already says it', () => {
    // 否则读起来是「会回应 被 @ 被 @」。
    expect(showSignalTag('at', '被 @')).toBe(false);
    expect(showSignalTag('name', '出现名字「咪啪」')).toBe(false);
    expect(showSignalTag('reply', '被回复，但本群的冷却还剩 29.0s')).toBe(false);
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

const base: PlatformDryRunVerdict = {
  index: 0,
  text: '今天好热',
  triggered: false,
  signal: 'llm_intent',
  blocked_by: '',
  reason: '',
  undetermined: true,
  cooldown_remaining: 0,
};

const chips = (extra: Partial<PlatformDryRunVerdict>) => verdictStatus({ ...base, ...extra }).map((chip) => chip.text);

describe('secondsText', () => {
  it('keeps one decimal', () => {
    expect(secondsText(4)).toBe('4s');
    expect(secondsText(2.25)).toBe('2.3s');
  });
});

describe('verdictStatus', () => {
  it('says nothing for an old server or a group without presence', () => {
    // 旧服务端不返回这些字段，状态列不能凭空编出来。
    expect(verdictStatus(base)).toEqual([]);
    expect(chips({ presence: '', quiet: '', gate: '', density_left: null, present_until: null })).toEqual([]);
  });

  it('shows presence and when it runs out', () => {
    expect(verdictStatus({ ...base, at_sec: 22, presence: 'present', present_until: 110 })).toEqual([
      { text: '在场', tone: 'live' },
      { text: '在场到 110s', tone: 'plain' },
    ]);
  });

  it('leaves out a presence window that has already ended', () => {
    expect(verdictStatus({ ...base, at_sec: 200, presence: 'absent', present_until: 110 })).toEqual([
      { text: '离场', tone: 'muted' },
    ]);
  });

  it('follows a message through the quiet wait', () => {
    expect(chips({ at_sec: 22, quiet: 'waiting', quiet_due_at: 26 })).toEqual(['安静等待中，26s 到点']);
    expect(chips({ quiet: 'merged' })).toEqual(['并进后面那条']);
    expect(chips({ at_sec: 27, quiet: 'due', quiet_due_at: 31, quiet_merged: 4 })).toEqual(['31s 到点，合并 4 条，拿这条问模型']);
    expect(chips({ at_sec: 50, quiet: 'due', quiet_due_at: 54, quiet_merged: 1 })).toEqual(['54s 到点，拿这条问模型']);
    expect(chips({ quiet: 'cancelled' })).toEqual(['等待被显式触发接走']);
  });

  it('shows the probability gate as a percentage and leaves the miss to the blocker', () => {
    expect(chips({ gate: 'pending', gate_probability: 0.1 })).toEqual(['概率门 10%，看运行时']);
    expect(chips({ gate: 'passed', gate_probability: 0.1 })).toEqual(['概率门掷中']);
    expect(chips({ gate: 'missed', gate_probability: 0.1 })).toEqual([]);
  });

  it('warns when the proactive replies run out', () => {
    expect(verdictStatus({ ...base, density_left: 2 })).toEqual([{ text: '主动回复还剩 2 次', tone: 'plain' }]);
    expect(verdictStatus({ ...base, density_left: 0 })).toEqual([{ text: '主动回复还剩 0 次', tone: 'danger' }]);
  });
});

describe('presenceSummary', () => {
  const presence: PlatformDryRunPresence = {
    active: true,
    window_sec: 90,
    absent_judge_probability: 0.1,
    quiet_wait_sec: 4,
    quiet_max_wait_sec: 20,
    density_limit: 3,
    density_window_sec: 300,
  };

  it('is empty when presence is off or the server is old', () => {
    expect(presenceSummary(undefined)).toBe('');
    expect(presenceSummary({ ...presence, active: false })).toBe('');
  });

  it('spells out the knobs in one line', () => {
    expect(presenceSummary(presence)).toBe(
      '在场窗口 90s · 离场时 10% 问模型 · 安静 4s 算停（最长等 20s） · 300s 内主动回复最多 3 次',
    );
  });

  it('says when proactive replies are unlimited', () => {
    expect(presenceSummary({ ...presence, density_limit: 0 })).toContain('主动回复不限次数');
  });
});
