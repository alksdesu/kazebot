// [2026-06-02] Small shared primitives for the expanded Settings pages.
// Why: eight new tabs share the same header, auth notice, card, status, and text
// control patterns. How: keep the repeated JSX in one local settings helper file.
// Purpose: new pages stay readable while preserving the existing Duties visual style.
import type { ReactNode } from 'react';
import yaml from 'js-yaml';

export const PageShell = ({ children }: { children: ReactNode }) => (
  <section className="h-full min-h-0 overflow-y-auto p-4 sm:p-6">
    <div className="mx-auto max-w-4xl space-y-5">{children}</div>
  </section>
);

export const PageHeader = ({ eyebrow, title, description, level = 1 }: { eyebrow?: string; title: string; description: string; level?: 1 | 2 }) => {
  const Heading = level === 1 ? 'h1' : 'h2';
  return (
  <header aria-label={eyebrow ? `${eyebrow}：${title}` : undefined}>
    <Heading className={`${level === 1 ? 'text-2xl' : 'text-xl'} font-semibold tracking-tight`}>{title}</Heading>
    <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--duties-secondary)]">{description}</p>
  </header>
  );
};

// 一条规则管到哪些渠道。写错过一次：QQ 的权限被当成浏览器设置，没人敢动。
export type SettingScope = 'all-channels' | 'this-browser';

const SCOPE_TEXT: Record<SettingScope, string> = {
  'all-channels': '所有渠道',
  'this-browser': '仅此浏览器',
};

export const ScopeBadge = ({ scope }: { scope: SettingScope }) => (
  <span
    className={`flex-shrink-0 rounded border px-2 py-0.5 text-xs ${
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
            <h2 className="text-base font-semibold">{title}</h2>
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
  message ? <p role="status" aria-live="polite" className="mt-2 text-sm leading-6 text-[var(--duties-secondary)]">{message}</p> : null
);

export const FieldLabel = ({ children, htmlFor }: { children: ReactNode; htmlFor?: string }) => (
  <label className="mb-1 block text-sm font-medium text-[var(--duties-secondary)]" htmlFor={htmlFor}>{children}</label>
);

export const TextInput = (props: React.InputHTMLAttributes<HTMLInputElement>) => (
  <input
    {...props}
    className={`w-full min-w-0 rounded border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)] ${props.className || ''}`}
  />
);

export const SelectInput = (props: React.SelectHTMLAttributes<HTMLSelectElement>) => (
  <select
    {...props}
    className={`w-full min-w-0 rounded border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)] ${props.className || ''}`}
  />
);

export function hasLikelyYamlSyntaxIssue(value: string): string {
  try { yaml.load(value); return ''; } catch (error) { return error instanceof Error ? error.message : 'YAML 语法错误'; }
}
