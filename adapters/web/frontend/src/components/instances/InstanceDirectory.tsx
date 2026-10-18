import { useEffect } from 'react';

import { clearInstanceToken, navigateToInstance, useRecentInstance } from '../../instances/navigation';
import { Button } from '../settings/pages/settingsControls';
import { useInstanceDirectory } from './useInstanceDirectory';

export function InstanceDirectory() {
  const { status, instances, currentPath, retry } = useInstanceDirectory();
  const recentPath = useRecentInstance();
  const recent = instances.find(instance => instance.path === recentPath);
  useEffect(clearInstanceToken, []);

  return <main className="flex min-h-dvh items-center justify-center bg-[var(--duties-bg)] px-4 py-8 text-[var(--duties-text)]">
    <section className="w-full max-w-2xl border border-[var(--duties-border)] bg-[var(--duties-surface)] p-6 sm:p-8" aria-labelledby="instance-directory-title">
      <header className="flex items-center gap-3">
        <img src={`${import.meta.env.BASE_URL}logo-sm.jpg`} alt="" className="h-11 w-11 rounded-lg" />
        <div><p className="text-xs font-medium tracking-wide text-[var(--duties-secondary)]">CLONOTH</p><h1 id="instance-directory-title" className="mt-1 text-2xl font-semibold">选择实例</h1></div>
      </header>
      <p className="mt-5 text-sm leading-relaxed text-[var(--duties-secondary)]">选择要进入的控制台。每个实例使用自己的管理员令牌，登录状态彼此独立。</p>
      {status === 'loading' && <p role="status" className="py-8 text-sm text-[var(--duties-secondary)]">正在读取实例目录…</p>}
      {status === 'error' && <div className="mt-6 space-y-4 border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
        <p role="alert" className="text-sm">暂时无法读取实例目录。可以重试，或直接进入当前实例。</p>
        <div className="flex flex-wrap gap-2"><Button onClick={retry}>重试</Button><Button tone="quiet" onClick={() => navigateToInstance(currentPath, false)}>进入当前实例</Button></div>
      </div>}
      {status === 'ready' && <div className="mt-6 space-y-5">
        {recent && <div className="flex flex-wrap items-center justify-between gap-3 border-l-4 border-[var(--duties-accent)] bg-[var(--duties-panel)] p-4">
          <div><p className="text-xs text-[var(--duties-secondary)]">最近使用</p><p className="mt-1 break-all font-medium">{recent.label}</p></div>
          <Button onClick={() => navigateToInstance(recent.path, false)}>继续进入 {recent.label}</Button>
        </div>}
        {instances.length ? <ul aria-label="可选实例" className="grid gap-3 sm:grid-cols-2">
          {instances.map(instance => <li key={instance.path}>
            <button type="button" onClick={() => navigateToInstance(instance.path, false)} aria-label={`进入 ${instance.label}`}
              className="group flex h-full w-full items-center justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-surface)] p-5 text-left transition-colors hover:border-[var(--duties-text)] hover:bg-[var(--duties-panel)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--duties-focus)]">
              <span className="min-w-0"><span className="block break-all text-lg font-semibold">{instance.label}</span><span className="mt-2 block text-sm text-[var(--duties-secondary)]">进入控制台</span></span>
              <span aria-hidden="true" className="shrink-0 text-lg text-[var(--duties-secondary)]">→</span>
            </button>
          </li>)}
        </ul> : <div className="space-y-3"><p role="status" className="text-sm text-[var(--duties-secondary)]">目录中暂时没有可选实例。</p><Button tone="quiet" onClick={() => navigateToInstance(currentPath, false)}>进入当前实例</Button></div>}
      </div>}
      <p className="mt-6 border-t border-[var(--duties-border)] pt-4 text-xs leading-relaxed text-[var(--duties-secondary)]">进入后可在登录页或侧栏切换实例。</p>
    </section>
  </main>;
}
