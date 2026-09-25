// This button component is added so the skeleton has one consistent Duties-themed control.
// It wraps the native button with minimal square styling, matching the no-rounded-corners design requirement.
// The purpose is to reuse hover, focus, and disabled states across sidebar and chat composer actions.
import type { ButtonHTMLAttributes, PropsWithChildren } from 'react';

type ButtonVariant = 'primary' | 'ghost' | 'danger';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  loading?: boolean;
}

const variantClassName: Record<ButtonVariant, string> = {
  primary: 'border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)] hover:bg-[var(--duties-accent)] hover:text-[var(--duties-text)]',
  ghost: 'border-[var(--duties-border)] bg-transparent text-[var(--duties-text)] hover:border-[var(--duties-text)] hover:bg-[var(--duties-accent)]',
  // [2026-06-02] Add an explicit dangerous action style for Settings controls.
  // Why: engine restart and approval denial can interrupt work or reject a pending
  // operation, so they should not look like neutral ghost buttons. How: expose a red
  // variant while preserving the same square Duties button shape. Purpose: callers
  // can mark destructive actions consistently without custom class strings.
  danger: 'border-[var(--duties-danger-border)] bg-[var(--duties-danger-bg)] text-[var(--duties-danger)] hover:bg-[var(--duties-danger-hover)]',
};

export const Button = ({ children, className = '', variant = 'ghost', type = 'button', loading = false, disabled, ...props }: PropsWithChildren<ButtonProps>) => (
  <button
    className={`app-button inline-flex min-h-10 items-center justify-center gap-2 border px-3 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${variantClassName[variant]} ${className}`}
    type={type}
    disabled={disabled || loading}
    aria-busy={loading || undefined}
    {...props}
  >
    {loading && <span aria-hidden="true" className="app-button-spinner" />}{children}
  </button>
);
