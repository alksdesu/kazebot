// 后端信号名 → 界面说法。trigger_policy 的 SIGNAL_* 是稳定字面量，这里只做展示映射。
// 认不出的名字原样显示：后端加了新信号时界面退化成英文标识，而不是漏掉一整行判定。

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
};

export const signalLabel = (name: string): string => SIGNAL_LABELS[name] || name || '无';

export const blockerLabel = (name: string): string => BLOCKER_LABELS[name] || name;

/** 后端的 reason 多数已经把命中的信号说出来了，再挂一个同名标签就是「会回应 被 @ 被 @」。 */
export function showSignalTag(signal: string, reason: string): boolean {
  if (!signal) return false;
  return !reason.includes(signalLabel(signal));
}
