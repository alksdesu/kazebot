// [2026-06-02] Regression tests for the remaining Settings optimizations.
// Why: the last Settings batch changes visibility, ordering, and selected-entry-node
// behavior across independent panels. How: render the relevant pages and right panels
// against mocked Supervisor responses. Purpose: future Settings edits can refactor
// layout without exposing advanced raw editors by default or losing the real node list.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ClientSettingsPage, parseNodeList } from '../components/settings/pages/ClientSettingsPage';
import { AgentsSettingsRightPanel, SkillsSettingsRightPanel } from '../components/settings/panels/SettingsContextPanels';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';
import type { AdminNode } from '../api/supervisorClient';

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

const nodeList: AdminNode[] = [
  { id: 'ereuna_main', type: 'ai', name: 'EreunaMain', description: '主入口', delegate_targets: ['worker_child'] },
  { id: 'worker_child', type: 'ai', name: 'Worker', description: '委派子节点' },
  { id: 'bootstrap.coder', type: 'ai', name: 'Coder', description: '编程入口' },
  { id: 'system.compactor', type: 'ai', name: 'Compactor', description: '系统节点' },
];

describe('remaining Settings optimizations', () => {
  beforeEach(() => {
    localStorage.clear();
    useSettingsStore.setState({
      adminToken: 'admin-token',
      isAuthenticated: true,
      entryNodeId: '',
      availableNodes: [],
      rightPanelOpen: true,
    });
    useSettingsSelectionStore.setState({
      selectedNode: null,
      selectedSkill: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('parses only root entry nodes from the real node list', () => {
    // [2026-06-02] Keep the filtering rule independent from the rendered select.
    // Why: several settings surfaces need the same root-entry-node semantics. How:
    // assert system nodes and delegated children are excluded from parseNodeList.
    // Purpose: the Client page does not show internal workers as default entries.
    expect(parseNodeList(nodeList).map((node) => node.id)).toEqual(['ereuna_main', 'bootstrap.coder']);
  });

  it('renders Client entry-node selection before approval rules and selects the configured node', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/v1/admin/config/all-tool-names')) return jsonResponse(['read_file', 'execute_command']);
      if (url.endsWith('/v1/admin/config/nodes')) return jsonResponse(nodeList);
      if (url.endsWith('/v1/config')) {
        return jsonResponse({
          version: 1,
          provider: 'openai',
          entry_node_id: 'ereuna_main',
          openai: { model: 'gpt-test', base_url: 'https://api.example.test/v1', api_key_present: true, api_key: '****' },
        });
      }
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ClientSettingsPage />);

    const entrySection = screen.getByRole('heading', { name: '入口节点' }).closest('section');
    const approvalSection = screen.getByRole('heading', { name: /自动审批规则/ }).closest('section');
    if (!entrySection || !approvalSection) throw new Error('settings sections not found');

    expect(entrySection.compareDocumentPosition(approvalSection) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const select = await screen.findByLabelText('入口节点');
    await waitFor(() => expect(select).toHaveValue('ereuna_main'));
    expect(within(select).getByRole('option', { name: /EreunaMain/ })).toBeInTheDocument();
    expect(within(select).queryByRole('option', { name: /Worker/ })).not.toBeInTheDocument();
    expect(within(select).queryByRole('option', { name: /Compactor/ })).not.toBeInTheDocument();
  });

  it('edits Agents as a structured form while keeping raw YAML collapsed', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'PUT') return jsonResponse({ ok: true });
      return jsonResponse({ content: 'id: ereuna_main\nname: EreunaMain\ndescription: 主入口\ntype: ai\nmodel: gpt-4.1\nprovider: openai\nmemory_book: ereuna\npersistent: true\ndelegate_targets:\n  - worker_child\ntool_access:\n  mode: allowlist\n  allow:\n    - read_file\n' });
    });
    vi.stubGlobal('fetch', fetchMock);
    useSettingsSelectionStore.setState({
      selectedNode: {
        id: 'ereuna_main',
        type: 'ai',
        name: 'EreunaMain',
        description: '主入口',
        delegate_targets: ['worker_child'],
        tool_access: { mode: 'all' },
      },
    });

    render(<AgentsSettingsRightPanel />);

    const nameInput = await screen.findByLabelText('名称');
    expect(nameInput).toHaveValue('EreunaMain');
    expect(screen.getByLabelText('节点 ID')).toHaveTextContent('ereuna_main');
    expect(screen.getByLabelText('工具权限模式')).toHaveValue('allowlist');
    expect(screen.getByLabelText('允许工具，使用英文逗号分隔')).toHaveValue('read_file');

    fireEvent.change(nameInput, { target: { value: 'Ereuna' } });
    fireEvent.change(screen.getByLabelText('委派目标，使用英文逗号分隔'), { target: { value: 'smith, scout' } });
    fireEvent.click(screen.getByRole('button', { name: '保存节点配置' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/nodes/ereuna_main/raw', expect.objectContaining({
      method: 'PUT',
      body: expect.stringContaining('name: Ereuna'),
    })));
    const savedYaml = JSON.parse(fetchMock.mock.calls.find((call) => String(call[0]).endsWith('/nodes/ereuna_main/raw') && call[1]?.method === 'PUT')?.[1]?.body as string).content as string;
    expect(savedYaml).toContain('  - smith\n  - scout');
    // 保存不能把白名单弄丢：曾经 mode 被降级成 all 且 allow 不写盘，节点从受限变成全部工具可用。
    expect(savedYaml).toContain('mode: allowlist');
    expect(savedYaml).toContain('- read_file');

    const details = screen.getByText('高级 YAML 编辑').closest('details');
    expect(details).not.toHaveAttribute('open');
    await waitFor(() => expect((screen.getByLabelText('节点 YAML 编辑器') as HTMLTextAreaElement).value).toContain('name: Ereuna'));
  });

  it('adds a collapsed raw Markdown editor to the Skills right panel and saves it', async () => {
    const rawMarkdown = '---\nenabled: true\nstrategy: normal\nkeywords: ["alpha"]\n---\n# Skill\n\nBody\n';
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'PUT') return jsonResponse({ ok: true });
      return jsonResponse({ content: rawMarkdown });
    });
    vi.stubGlobal('fetch', fetchMock);
    useSettingsSelectionStore.setState({
      selectedSkill: { name: 'skill_alpha', enabled: true, strategy: 'normal', keywords: ['alpha'] },
    });

    render(<SkillsSettingsRightPanel />);

    const details = screen.getByText('Raw Markdown 编辑（高级）').closest('details');
    expect(details).not.toHaveAttribute('open');
    if (!details) throw new Error('raw markdown details not found');

    fireEvent.click(within(details).getByText('Raw Markdown 编辑（高级）'));
    const editor = await screen.findByLabelText('技能 Raw Markdown 编辑器');
    expect(editor).toHaveValue(rawMarkdown);

    fireEvent.change(editor, { target: { value: `${rawMarkdown}\nExtra line\n` } });
    fireEvent.click(within(details).getByRole('button', { name: '保存 Raw Markdown' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/skills/skill_alpha/raw', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ content: `${rawMarkdown}\nExtra line\n` }),
    })));
  });
});
