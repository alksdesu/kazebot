// 语义色的成套 class：框、底、字一起换。同属性的 arbitrary utility 叠在一个元素上，谁赢看 Tailwind 出 CSS 的顺序。
export type Tone = 'success' | 'danger' | 'warning' | 'info' | 'meta' | 'neutral';

/** 有框有底的一块：状态标签、提示条、工具卡片、带底色的按钮。 */
export const TONE_BOX: Record<Tone, string> = {
  success: 'border-[var(--duties-success-border)] bg-[var(--duties-success-bg)] text-[var(--duties-success)]',
  danger: 'border-[var(--duties-danger-border)] bg-[var(--duties-danger-bg)] text-[var(--duties-danger)]',
  warning: 'border-[var(--duties-warning-border)] bg-[var(--duties-warning-bg)] text-[var(--duties-warning)]',
  info: 'border-[var(--duties-info-border)] bg-[var(--duties-info-bg)] text-[var(--duties-info)]',
  meta: 'border-[var(--duties-meta-border)] bg-[var(--duties-meta-bg)] text-[var(--duties-meta)]',
  neutral: 'border-[var(--duties-border)] bg-[var(--duties-bg)] text-[var(--duties-tertiary)]',
};
