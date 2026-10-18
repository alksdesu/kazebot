import { extensions, useExtensionRevision } from './registry';
import type { WorkspaceDefinition } from './sdk';

const builtins: WorkspaceDefinition[] = [
  { id: 'chat', label: '聊天', icon: 'chat', order: 0 },
  { id: 'execution', label: '任务', icon: 'checklist', order: 10 },
  { id: 'materials', label: '资料', icon: 'folder_open', order: 20 },
];
for (const workspace of builtins) extensions.builtin('workspaces', workspace.id, workspace);
export function useWorkspaces() {
  useExtensionRevision();
  return extensions.ids('workspaces').map((id) => extensions.resolve<WorkspaceDefinition>('workspaces', id)!).sort((a, b) => (a.order || 0) - (b.order || 0) || a.id.localeCompare(b.id));
}
