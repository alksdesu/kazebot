import { useId } from 'react';

import { clearInstanceToken, instanceEntryHref, navigateToInstance } from '../../instances/navigation';
import { useInstanceDirectory } from './useInstanceDirectory';

export function InstanceNavigation() {
  const { status, instances, currentPath, retry } = useInstanceDirectory();
  const selectId = useId();
  const current = instances.find(instance => instance.path === currentPath);

  return <nav aria-label="实例导航" className="space-y-2 border-b border-[var(--duties-border)] px-4 py-3">
    <div className="flex min-w-0 items-baseline justify-between gap-2 text-xs">
      <span className="shrink-0 text-[var(--duties-secondary)]">当前实例</span>
      <span className="truncate font-medium" title={current?.label}>{current?.label ?? (status === 'loading' ? '读取中…' : currentPath || '主实例')}</span>
    </div>
    {status === 'ready' && instances.length > 1 && <div>
      <label htmlFor={selectId} className="sr-only">切换实例</label>
      <select id={selectId} className="app-input w-full min-w-0 text-sm" value={currentPath} onChange={event => {
        const target = event.currentTarget.value;
        event.currentTarget.value = currentPath;
        if (target !== currentPath) navigateToInstance(target);
      }}>
        {!current && <option value={currentPath}>当前实例</option>}
        {instances.map(instance => <option key={instance.path} value={instance.path}>{instance.label}</option>)}
      </select>
    </div>}
    {status === 'loading' && <p role="status" className="sr-only">正在读取实例目录…</p>}
    {status === 'error' && <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[var(--duties-secondary)]">
      <a className="underline underline-offset-2" href={instanceEntryHref()} referrerPolicy="no-referrer" onClick={clearInstanceToken}>选择实例</a>
      <button type="button" onClick={retry} className="underline underline-offset-2">重试实例目录</button>
    </div>}
  </nav>;
}
