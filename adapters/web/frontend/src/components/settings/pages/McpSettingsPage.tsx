// [2026-06-02] MCP client settings page.
// Why: MCP clients are configured in one YAML file but operators need a parsed list
// and a field editor. How: show only the client list and selection actions in the
// center page, then let the Settings right panel edit and save the selected client.
// Purpose: MCP configuration is easier to scan while raw writeback remains available.
import { useEffect, useRef, useState } from 'react';

import { getMcpClients, getMcpClientsRaw, updateMcpClientsRaw, type McpClient } from '../../../api/supervisorClient';
import { usePageNotice } from '../../../hooks/usePageNotice';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { tokenChanged, useSettingsStore } from '../../../store/settingsStore';
import { confirmNavigation } from '../../../hooks/useUnsavedChanges';
import { Block, Button, Field, Input, PageShell, StatusText } from './settingsControls';
import { parseMcpClients, serializeMcpClients, type McpClientFormState } from '../settingsStructuredConfig';

function emptyMcpClient(id: string): McpClientFormState {
  // [2026-06-02] Create a valid MCP client draft for the form writer. Why: the
  // backend has only raw YAML endpoints for MCP clients. How: append a normalized
  // streamable_http client with empty URL and headers. Purpose: new clients can be
  // created from the list page and then completed in the right panel.
  return { id, description: '通过设置页面创建的 MCP Client。', enabled: false, transport: 'streamable_http', command: '', argsText: '', envText: '', url: '', headersText: '' };
}

export const McpSettingsPage = () => {
  // [2026-06-02] Pull the right-panel opener into the list page. Why: selecting an
  // MCP client on mobile should reveal the connection editor immediately. How: call
  // the shared settings-store setter from each row click. Purpose: users do not need a
  // second tap on the small header chevron after choosing an item.
  const { adminToken, isAuthenticated, setRightPanelOpen } = useSettingsStore();
  const { selectedMcpClient, setSelectedMcpClient } = useSettingsSelectionStore();
  const [clients, setClients] = useState<McpClient[]>([]);
  const [newId, setNewId] = useState('');
  const { notice, setMessage, setLoadError, clearNotice } = usePageNotice();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const mutation = useRef(false);
  const generation = useRef(0);

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    const version = ++generation.current;
    setLoading(true);
    try {
      const items = await getMcpClients(adminToken);
      if (version !== generation.current) return;
      setClients(items);
      if (selectedMcpClient && !items.some((item) => item.id === selectedMcpClient.id)) setSelectedMcpClient(null);
      setLoadError('');
    } catch (error) {
      if (version !== generation.current) return;
      setLoadError(error instanceof Error ? error.message : '加载 MCP 配置失败');
    } finally {
      if (version === generation.current) setLoading(false);
    }
  };

  // Clear the notice only on an explicit refresh: create and delete reload right after writing theirs.
  const refresh = async () => {
    clearNotice();
    await load();
  };

  useEffect(() => { void refresh(); return () => { generation.current += 1; }; }, [adminToken, isAuthenticated]);

  useEffect(() => {
    // [2026-06-02] Refresh parsed MCP list after the right-panel form saves raw YAML.
    // Why: the page no longer owns the MCP YAML editor. How: reload on a local browser
    // event emitted by the right panel. Purpose: transport, URL, and enabled previews
    // stay current after editing.
    const handler = () => { void load(); };
    window.addEventListener('settings:mcp-updated', handler);
    return () => window.removeEventListener('settings:mcp-updated', handler);
  }, [adminToken, isAuthenticated, selectedMcpClient?.id]);

  const create = async () => {
    if (!adminToken || mutation.current || loading) return;
    const id = newId.trim();
    if (!id) { setMessage('请输入 MCP Client ID'); return; }
    if (!confirmNavigation()) return;
    mutation.current = true; setBusy(true);
    try {
      const file = await getMcpClientsRaw(adminToken);
      if (tokenChanged(adminToken)) return;
      const forms = parseMcpClients(file.content);
      if (forms.some((client) => client.id === id)) { setMessage('该 MCP Client 已存在'); return; }
      const nextForms = [...forms, emptyMcpClient(id)];
      await updateMcpClientsRaw(adminToken, serializeMcpClients(nextForms, file.content), file.hash);
      if (tokenChanged(adminToken)) return;
      setNewId('');
      setMessage('Client 已创建但尚未启用，请在右栏填写连接信息后启用。');
      await load();
      if (tokenChanged(adminToken)) return;
      setSelectedMcpClient({ id, enabled: false, transport: 'streamable_http', description: '通过设置页面创建的 MCP Client。' });
      setRightPanelOpen(true);
    } catch (error) {
      if (tokenChanged(adminToken)) return;
      setMessage(error instanceof Error ? error.message : '创建 MCP Client 失败');
    } finally { mutation.current = false; setBusy(false); }
  };

  const remove = async () => {
    if (!adminToken || !selectedMcpClient || mutation.current || loading) return;
    if (!window.confirm(`确定要删除 MCP Client ${selectedMcpClient.id} 吗？`)) return;
    if (!confirmNavigation()) return;
    mutation.current = true; setBusy(true);
    try {
      const file = await getMcpClientsRaw(adminToken);
      if (tokenChanged(adminToken)) return;
      const nextForms = parseMcpClients(file.content).filter((client) => client.id !== selectedMcpClient.id);
      await updateMcpClientsRaw(adminToken, serializeMcpClients(nextForms, file.content), file.hash);
      if (tokenChanged(adminToken)) return;
      setSelectedMcpClient(null);
      setMessage('MCP Client 已删除');
      await load();
    } catch (error) {
      if (tokenChanged(adminToken)) return;
      setMessage(error instanceof Error ? error.message : '删除 MCP Client 失败');
    } finally { mutation.current = false; setBusy(false); }
  };

  const preview = (client: McpClient): string => {
    if (client.transport === 'stdio') return String(client.command || '未设置命令');
    return String(client.url || '未设置 URL');
  };

  return (
    <PageShell authed={isAuthenticated} description="查看 MCP Client 列表，选择后在右栏编辑连接字段和保存。" title="MCP">
      <Block frame="boxed" title="Client 列表" hint="展示 ID、传输方式、启用状态和连接预览。编辑表单位于右栏。">
        <div className="mb-3 flex flex-wrap gap-2">
          <Button tone="quiet" disabled={loading || busy} onClick={() => void refresh()}>{loading ? '正在刷新…' : '刷新 MCP'}</Button>
          <Button tone="danger" disabled={loading || busy || !selectedMcpClient} onClick={remove}>删除选中 Client</Button>
        </div>
        <div className="max-h-[var(--duties-pane-height-tall)] space-y-2 overflow-y-auto">
          {clients.length === 0 ? <p className="text-sm text-[var(--duties-secondary)]">暂无 MCP Client。</p> : clients.map((client) => (
            <button className={`w-full border p-3 text-left ${selectedMcpClient?.id === client.id ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`} key={client.id} disabled={busy} onClick={() => { setSelectedMcpClient(client); setMessage(''); setRightPanelOpen(true); }} type="button">
              <div className="flex flex-wrap items-center gap-2"><span className="font-mono text-xs font-semibold">{client.id}</span><span className={`border px-1.5 py-0.5 text-xs ${client.enabled === false ? 'border-[var(--duties-border)] text-[var(--duties-secondary)]' : 'border-[var(--duties-border)] text-[var(--duties-success)]'}`}>{client.enabled === false ? '禁用' : '启用'}</span><span className="font-mono text-xs text-[var(--duties-tertiary)]">{client.transport || '未知传输'}</span></div>
              <p className="mt-1 truncate text-xs text-[var(--duties-secondary)]">{preview(client)}</p>
            </button>
          ))}
        </div>
        <div className="mt-4 border-t border-[var(--duties-border)] pt-3">
          <Field htmlFor="new-mcp-id" label="创建 MCP Client" layout="stacked">
            <Input width="wide" disabled={busy} id="new-mcp-id" onChange={(event) => setNewId(event.target.value)} placeholder="Client ID" value={newId} />
            <Button className="mt-2" disabled={busy || loading || !newId.trim()} onClick={create}>创建 Client</Button>
          </Field>
        </div>
        <StatusText message={notice} />
      </Block>
    </PageShell>
  );
};
