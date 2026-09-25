import { useViewStore } from '../store/viewStore';
import { Icon } from '../components/common';

export function WorkspaceNav() {
  const { viewMode, openWorkspace } = useViewStore();
  return <nav aria-label="工作区" className="grid min-w-0 grid-cols-3 gap-1 border-b border-[var(--duties-border)] p-2">
    {([['chat', '聊天', 'chat'], ['execution', '任务', 'checklist'], ['materials', '资料', 'folder_open']] as const).map(([view, label, icon]) => <button
      key={view} type="button" aria-current={viewMode === view ? 'page' : undefined}
      className={`flex min-h-14 min-w-0 flex-col items-center justify-center gap-1 border px-2 py-2 text-sm transition-colors ${viewMode === view ? 'border-[var(--duties-text)] bg-[var(--duties-muted)] font-semibold' : 'border-transparent hover:bg-[var(--duties-muted)]'}`}
      onClick={() => openWorkspace(view)}><Icon name={icon} size={16} /><span className="whitespace-nowrap">{label}</span></button>)}
  </nav>;
}
