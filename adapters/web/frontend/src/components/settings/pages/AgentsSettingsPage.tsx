import { confirmNavigation } from '../../../hooks/useUnsavedChanges';
import { useExclusiveAction } from '../../../hooks/useExclusiveAction';
import { usePageNotice } from '../../../hooks/usePageNotice';
// [2026-06-02] Agent and node management settings page.
// Why: node YAML files are core runtime assets and need a first-class editor. How:
// list configured nodes, support template-based creation and deletion, and keep the
// raw YAML editor in the independent Settings right panel. Purpose: the center page
// stays focused on selection and operations while the right rail owns editing.
import { useEffect, useRef, useState } from 'react';

import { createNode, deleteNode, getNodes, type AdminNode } from '../../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { tokenChanged, useSettingsStore } from '../../../store/settingsStore';
import { describeEnvRef } from '../../../utils/envRef';
import { Block, Button, Field, Input, PageShell, Select, StatusText } from './settingsControls';

function nodeTemplate(id: string, template: string): string {
  // [2026-06-02] Build a minimal node YAML from the selected template type. Why: the
  // backend create endpoint accepts complete content, not a template identifier. How:
  // provide safe starter YAML with comments explaining required follow-up edits.
  // Purpose: users can create a valid file and then refine it in the right panel.
  const base = `# [2026-06-02] Created from Settings. Why: the web UI creates raw node YAML from a compact template. How: edit this file after creation to match the node role. Purpose: keep node creation explicit and reviewable.\nid: ${id}\nname: ${id}\ndescription: 通过设置页面创建的节点。\n`;
  if (template === 'tool') return `${base}type: tool\nmodel: ""\ntool_access:\n  mode: none\ndelegate_targets: []\n`;
  return `${base}type: ai\nmodel: ""\ntool_access:\n  mode: all\ndelegate_targets: []\n`;
}

export const AgentsSettingsPage = () => {
  // [2026-06-02] Pull the right-panel opener into the list page. Why: selecting a
  // node on mobile should reveal the YAML editor immediately. How: call the shared
  // settings-store setter from each row click. Purpose: users do not need a second tap
  // on the small header chevron after choosing an item.
  const { adminToken, isAuthenticated, setRightPanelOpen } = useSettingsStore();
  const { selectedNode, setSelectedNode } = useSettingsSelectionStore();
  const [nodes, setNodes] = useState<AdminNode[]>([]);
  const [newId, setNewId] = useState('');
  const [template, setTemplate] = useState('ai');
  const { notice, setMessage, setLoadError, clearNotice } = usePageNotice();
  const [loading, setLoading] = useState(false);
  const { busy, run } = useExclusiveAction();
  const generation = useRef(0);

  const loadNodes = async () => {
    if (!adminToken || !isAuthenticated) return;
    const version = ++generation.current;
    setLoading(true);
    try {
      const items = await getNodes(adminToken);
      if (version !== generation.current) return;
      setNodes(items);
      if (selectedNode && !items.some((item) => item.id === selectedNode.id)) setSelectedNode(null);
      setLoadError('');
    } catch (error) {
      if (version !== generation.current) return;
      setLoadError(error instanceof Error ? error.message : '加载节点失败');
    } finally {
      if (version === generation.current) setLoading(false);
    }
  };

  // Clear the notice only on an explicit refresh: create and delete reload right after writing theirs.
  const refresh = async () => {
    clearNotice();
    await loadNodes();
  };

  useEffect(() => { void refresh(); return () => { generation.current += 1; }; }, [adminToken, isAuthenticated]);

  useEffect(() => {
    // [2026-06-02] Refresh the list after the right panel saves raw YAML. Why: the
    // editor lives outside this page, so a browser event is the smallest shared signal.
    // How: listen for a local settings event and reload parsed node metadata. Purpose:
    // saved names, models, and delegate summaries can appear without leaving the tab.
    const handler = () => { void loadNodes(); };
    window.addEventListener('settings:nodes-updated', handler);
    return () => window.removeEventListener('settings:nodes-updated', handler);
  }, [adminToken, isAuthenticated, selectedNode?.id]);

  const create = () => run(async () => {
    if (!adminToken) return;
    const id = newId.trim();
    if (!id) { setMessage('请输入节点 ID'); return; }
    if (!confirmNavigation()) return;
    try {
      await createNode(adminToken, { id, content: nodeTemplate(id, template) });
      if (tokenChanged(adminToken)) return;
      setNewId('');
      setMessage('节点已创建，请在右栏继续编辑 YAML。');
      await loadNodes();
      if (tokenChanged(adminToken)) return;
      setSelectedNode({ id, name: id, type: template === 'tool' ? 'tool' : 'ai', model: '', description: '通过设置页面创建的节点。', delegate_targets: [] } as AdminNode);
    } catch (error) {
      if (tokenChanged(adminToken)) return;
      setMessage(error instanceof Error ? error.message : '创建节点失败');
    }
  });
  const remove = () => run(async () => {
    if (!adminToken || !selectedNode) return;
    if (!window.confirm(`确定要删除节点 ${selectedNode.id} 吗？此操作会删除对应 YAML 文件。`)) return;
    if (!confirmNavigation()) return;
    try {
      await deleteNode(adminToken, selectedNode.id);
      if (tokenChanged(adminToken)) return;
      setSelectedNode(null);
      setMessage('节点已删除');
      await loadNodes();
    } catch (error) {
      if (tokenChanged(adminToken)) return;
      setMessage(error instanceof Error ? error.message : '删除节点失败');
    }
  });
  return (
    <PageShell authed={isAuthenticated} description="查看、创建和删除节点配置文件。选择节点后，请在右侧面板编辑对应 YAML。" title="节点管理">
      <Block frame="boxed" title="节点列表" hint="中间区域只负责列表和操作，节点 YAML 编辑器显示在右栏。">
        <div className="mb-3 flex flex-wrap gap-2">
          <Button tone="quiet" disabled={loading || busy} onClick={() => void refresh()}>{loading ? '正在刷新…' : '刷新节点'}</Button>
          <Button tone="danger" disabled={busy || !selectedNode} onClick={remove}>删除选中节点</Button>
        </div>
        <div className="max-h-[var(--duties-pane-height-tall)] space-y-2 overflow-y-auto">
          {nodes.map((node) => (
            <button className={`w-full border p-3 text-left ${selectedNode?.id === node.id ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`} key={node.id} disabled={busy} onClick={() => { setSelectedNode(node); setMessage(''); setRightPanelOpen(true); }} type="button">
              <p className="flex flex-wrap items-center gap-1.5 font-mono text-xs font-semibold">
                {node.id}
                {node.active === false && (
                  <span className="border border-[var(--duties-border)] px-1.5 py-0.5 text-nano font-normal text-[var(--duties-secondary)]">
                    模板，派发不到
                  </span>
                )}
              </p>
              {node.declared_id && (
                // engine 拿 node_id 直接拼文件名找文件，yaml 里这个 id 它不读。
                <p className="mt-1 text-xs text-[var(--duties-danger)]">
                  文件里写着 id: {node.declared_id}，但节点认的是文件名，改那一行不会换节点
                </p>
              )}
              <p className="mt-1 text-xs text-[var(--duties-secondary)]">{node.name || '未命名'} · {node.type || '未知类型'} · {describeEnvRef(node.model || '') || '未设置模型'}</p>
              {node.description && <p className="mt-1 line-clamp-2 text-xs leading-5 text-[var(--duties-secondary)]">{node.description}</p>}
            </button>
          ))}
        </div>
        <div className="mt-4 border-t border-[var(--duties-border)] pt-3">
          <Field htmlFor="new-node-id" label="创建节点" layout="stacked">
            <Input width="wide" disabled={busy} id="new-node-id" onChange={(event) => setNewId(event.target.value)} placeholder="节点 ID" value={newId} />
            <Select width="wide" aria-label="新节点模板" disabled={busy} className="mt-2" onChange={(event) => setTemplate(event.target.value)} value={template}>
              <option value="ai">AI 节点模板</option>
              <option value="tool">工具节点模板</option>
            </Select>
            <Button className="mt-2" disabled={busy} onClick={create}>创建节点</Button>
          </Field>
        </div>
        <StatusText message={notice} />
      </Block>
    </PageShell>
  );
};
