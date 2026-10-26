// 后端的信号名、拦截原因和在场状态 → 界面说法。trigger_policy 里这些都是稳定字面量，这里只做展示映射。
// 认不出的名字原样显示：后端加了新信号时界面退化成英文标识，而不是漏掉一整行判定。
import type { PlatformDryRunPresence, PlatformDryRunVerdict } from '../api/supervisorClient';
import { asPercent } from './format';

const SIGNAL_LABELS: Record<string, string> = {
  all: '全量回应',
  at: '被 @',
  reply: '被回复',
  name: '出现名字',
  prefix: '前缀触发',
  keyword: '关键词插话',
  random: '随机插话',
  llm_intent: '模型判断',
};

const BLOCKER_LABELS: Record<string, string> = {
  cooldown: '冷却',
  gate: '概率门',
  density: '主动回复上限',
};

export const signalLabel = (name: string): string => SIGNAL_LABELS[name] || name || '无';

export const blockerLabel = (name: string): string => BLOCKER_LABELS[name] || name;

/** 后端的 reason 多数已经把命中的信号说出来了，再挂一个同名标签就是「会回应 被 @ 被 @」。 */
export function showSignalTag(signal: string, reason: string): boolean {
  if (!signal) return false;
  return !reason.includes(signalLabel(signal));
}

export type StatusTone = 'live' | 'plain' | 'muted' | 'danger';

export interface StatusChip {
  text: string;
  tone: StatusTone;
}

/** 试听时间线上的秒数，留一位小数。 */
export const secondsText = (value: number): string => `${Math.round(value * 10) / 10}s`;

/** 在场判断带出来的状态，一项一枚短标签：在不在场、安静等待的去向、概率门、主动回复还剩几次。 */
export function verdictStatus(verdict: PlatformDryRunVerdict): StatusChip[] {
  const chips: StatusChip[] = [];
  const at = verdict.at_sec ?? 0;
  if (verdict.presence === 'present') chips.push({ text: '在场', tone: 'live' });
  if (verdict.presence === 'absent') chips.push({ text: '离场', tone: 'muted' });
  if (typeof verdict.present_until === 'number' && verdict.present_until > at) {
    chips.push({ text: `在场到 ${secondsText(verdict.present_until)}`, tone: 'plain' });
  }
  const due = secondsText(verdict.quiet_due_at ?? at);
  const merged = verdict.quiet_merged ?? 1;
  if (verdict.quiet === 'waiting') chips.push({ text: `安静等待中，${due} 到点`, tone: 'plain' });
  if (verdict.quiet === 'merged') chips.push({ text: '并进后面那条', tone: 'muted' });
  if (verdict.quiet === 'due') {
    chips.push({ text: `${due} 到点${merged > 1 ? `，合并 ${merged} 条` : ''}，拿这条问模型`, tone: 'live' });
  }
  if (verdict.quiet === 'cancelled') chips.push({ text: '等待被显式触发接走', tone: 'muted' });
  if (verdict.gate === 'pending') {
    chips.push({ text: `概率门 ${asPercent(verdict.gate_probability ?? 0)}%，看运行时`, tone: 'plain' });
  }
  if (verdict.gate === 'passed') chips.push({ text: '概率门掷中', tone: 'live' });
  if (typeof verdict.density_left === 'number') {
    chips.push({ text: `主动回复还剩 ${verdict.density_left} 次`, tone: verdict.density_left > 0 ? 'plain' : 'danger' });
  }
  return chips;
}

/** 试听头部的一句在场判断配置；没开在场判断时是空串。 */
export function presenceSummary(presence: PlatformDryRunPresence | undefined): string {
  if (!presence?.active) return '';
  const density = presence.density_limit > 0
    ? `${secondsText(presence.density_window_sec)} 内主动回复最多 ${presence.density_limit} 次`
    : '主动回复不限次数';
  return [
    `在场窗口 ${secondsText(presence.window_sec)}`,
    `离场时 ${asPercent(presence.absent_judge_probability)}% 问模型`,
    `安静 ${secondsText(presence.quiet_wait_sec)} 算停（最长等 ${secondsText(presence.quiet_max_wait_sec)}）`,
    density,
  ].join(' · ');
}
