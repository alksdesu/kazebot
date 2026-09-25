import type { ReactNode } from 'react';
import { PageHeader } from '../components/settings/pages/settingsPagePrimitives';

export const controlClass = 'block w-full min-w-0 max-w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 text-sm text-[var(--duties-text)] disabled:cursor-not-allowed disabled:opacity-50';
export const actionClass = 'inline-flex min-h-10 max-w-full items-center justify-center border border-[var(--duties-border)] px-3 py-2 text-sm transition-colors hover:bg-[var(--duties-muted)] disabled:cursor-not-allowed disabled:opacity-50';
export const primaryClass = `${actionClass} border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)] hover:bg-[var(--duties-secondary)]`;
export const sectionClass = 'min-w-0 space-y-4 border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4';

export function FeaturePage({ title, description, embedded = false, children }: { title: string; description: string; embedded?: boolean; children: ReactNode }) {
  return <section className={`${embedded ? '' : 'h-full min-h-0 overflow-y-auto p-4 sm:p-6'} min-w-0 text-[var(--duties-text)]`}>
    <div className="mx-auto min-w-0 max-w-5xl space-y-6 text-sm [overflow-wrap:anywhere]">
      <PageHeader eyebrow="工作区" title={title} description={description} level={embedded ? 2 : 1} />{children}
    </div>
  </section>;
}

export function FeatureFeedback({ error, notice, retry }: { error?: string; notice?: string; retry?: () => void }) {
  return <>{error && <div role="alert" className={`${sectionClass} border-[var(--duties-danger)]`}><p>{error}</p>{retry && <button type="button" className={actionClass} onClick={retry}>重试读取</button>}</div>}
    {notice && <p role="status" className="border-l-2 border-[var(--duties-text)] pl-3">{notice}</p>}</>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="border border-dashed border-[var(--duties-border)] p-4 leading-relaxed text-[var(--duties-secondary)]">{children}</p>;
}
