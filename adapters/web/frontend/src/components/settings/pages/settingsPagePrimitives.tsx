// [2026-06-02] Small shared primitives for the expanded Settings pages.
// Why: eight new tabs share the same header, auth notice, card, status, and text
// control patterns. How: keep the repeated JSX in one local settings helper file.
// Purpose: new pages stay readable while preserving the existing Duties visual style.
import type { ReactNode } from 'react';

export const PageShell = ({ children }: { children: ReactNode }) => (
  <section className="h-full min-h-0 overflow-y-auto p-4 sm:p-6">
    <div className="mx-auto max-w-4xl space-y-5">{children}</div>
  </section>
);

export const PageHeader = ({ eyebrow = '设置', title, description }: { eyebrow?: string; title: string; description: string }) => (
  <header>
    <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">{eyebrow}</p>
    <h1 className="mt-2 font-mono text-xl font-semibold tracking-[-0.04em]">{title}</h1>
    <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--duties-secondary)]">{description}</p>
  </header>
);

// 一条规则管到哪些渠道。写错过一次：QQ 的权限被当成浏览器设置，没人敢动。
export type SettingScope = 'all-channels' | 'this-browser';

const SCOPE_TEXT: Record<SettingScope, string> = {
  'all-channels': '所有渠道',
  'this-browser': '仅此浏览器',
};

export const ScopeBadge = ({ scope }: { scope: SettingScope }) => (
  <span
    className={`flex-shrink-0 border px-1.5 py-0.5 font-mono text-[0.55rem] tracking-[0.08em] ${
      scope === 'all-channels'
        ? 'border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)]'
        : 'border-[var(--duties-border)] text-[var(--duties-secondary)]'
    }`}
  >
    {SCOPE_TEXT[scope]}
  </span>
);

export const Card = ({ title, description, scope, children }: { title?: string; description?: string; scope?: SettingScope; children: ReactNode }) => (
  <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
    {(title || description) && (
      <div className="mb-3">
        {title && (
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-mono text-sm font-semibold">{title}</h2>
            {scope && <ScopeBadge scope={scope} />}
          </div>
        )}
        {description && <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">{description}</p>}
      </div>
    )}
    {children}
  </section>
);

export const AuthRequired = () => (
  <Card>
    {/* [2026-06-02] Use the exact cross-tab authentication guidance requested for
        P0 Settings pages. Why: System, Approvals, and Advanced depend on protected
        Admin API endpoints and should direct users to the one login location. How:
        render the same Chinese sentence from the shared helper. Purpose: every
        protected settings page has consistent empty-auth copy. */}
    <p className="text-sm leading-6 text-[var(--duties-secondary)]">请先在通用页面登录 Admin Token</p>
  </Card>
);

export const StatusText = ({ message }: { message: string }) => (
  message ? <p className="mt-2 text-xs leading-5 text-[var(--duties-tertiary)]">{message}</p> : null
);

export const FieldLabel = ({ children, htmlFor }: { children: ReactNode; htmlFor?: string }) => (
  <label className="mb-1 block text-xs font-semibold text-[var(--duties-secondary)]" htmlFor={htmlFor}>{children}</label>
);

export const TextInput = (props: React.InputHTMLAttributes<HTMLInputElement>) => (
  <input
    {...props}
    className={`w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)] ${props.className || ''}`}
  />
);

export const SelectInput = (props: React.SelectHTMLAttributes<HTMLSelectElement>) => (
  <select
    {...props}
    className={`w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)] ${props.className || ''}`}
  />
);

export function hasLikelyYamlSyntaxIssue(value: string): string {
  // [2026-06-02] Lightweight frontend YAML check. Why: js-yaml is not installed and
  // adding a dependency is unnecessary for this task. How: catch leading tab
  // indentation and unclosed quotes before saving, then let the backend perform final
  // validation. Purpose: users get immediate feedback for common raw-config mistakes.
  if (value.split('\n').some((line) => /^\t+/.test(line))) return 'YAML 缩进不能使用制表符。';
  const singleQuotes = (value.match(/'/g) || []).length;
  const doubleQuotes = (value.match(/"/g) || []).length;
  if (singleQuotes % 2 === 1 || doubleQuotes % 2 === 1) return '文本中可能存在未闭合的引号。';
  return '';
}
