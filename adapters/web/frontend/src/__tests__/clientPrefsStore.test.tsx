// 本机偏好：自动审批、标题生成、渲染折叠都只写 localStorage，后端策略碰不到它们。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAllToolNames } from '../api/supervisorClient';
import { AutoApproveSection } from '../components/settings/pages/AutoApproveSection';
import { ClientSettingsPage } from '../components/settings/pages/ClientSettingsPage';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';
import {
  migrateAutoApproveRules,
  shouldAutoApproveToolCall,
  useClientPrefsStore,
} from '../store/clientPrefsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getAllToolNames: vi.fn(),
}));

// 后端实际会返回的量级：25 个内置 + cancel_active_tasks + 外部脚本工具。
const BUILTIN_TOOL_NAMES = [
  'apply_diff', 'cancel_active_tasks', 'create_agent', 'create_or_update_mcp_client',
  'create_or_update_skill', 'create_or_update_tool', 'create_schedule', 'delete_memory',
  'delete_mcp_client', 'delete_schedule', 'delete_skill', 'execute_command', 'get_context_window',
  'list_dir', 'list_memories', 'list_mcp_clients', 'list_schedules', 'list_skills',
  'manage_secret', 'read_file', 'reload_tools', 'request_restart', 'save_memory',
  'search_in_files', 'write_file',
];
const EXTERNAL_TOOL_NAMES = ['gemini_image', 'weather_query', 'zlibrary_search'];
const ALL_TOOL_NAMES = [...BUILTIN_TOOL_NAMES, ...EXTERNAL_TOOL_NAMES];

describe('client preferences store and page', () => {
  beforeEach(() => {
    localStorage.clear();
    useClientPrefsStore.getState().resetClientPrefs();
    useSettingsSelectionStore.setState({ allToolNames: [] });
    useSettingsStore.setState({ adminToken: null, isAuthenticated: false, availableNodes: [], modelConfig: null });
    vi.mocked(getAllToolNames).mockResolvedValue([...ALL_TOOL_NAMES]);
  });

  afterEach(() => {
    localStorage.clear();
    useClientPrefsStore.getState().resetClientPrefs();
    useSettingsSelectionStore.setState({ allToolNames: [] });
    useSettingsStore.setState({ adminToken: null, isAuthenticated: false, availableNodes: [], modelConfig: null });
    vi.restoreAllMocks();
  });

  it('uses safe defaults for known and unknown tool approval rules', () => {
    expect(shouldAutoApproveToolCall({ toolName: 'read_file', operation: 'read_file' }, {})).toBe(true);
    expect(shouldAutoApproveToolCall({ toolName: 'search_in_files', operation: 'read_file' }, {})).toBe(true);
    expect(shouldAutoApproveToolCall({ toolName: 'list_dir', operation: 'read_file' }, {})).toBe(true);
    expect(shouldAutoApproveToolCall({ toolName: 'execute_command', operation: 'execute_command' }, {})).toBe(false);
    expect(shouldAutoApproveToolCall({ toolName: 'unknown_tool', operation: 'unknown_tool' }, {})).toBe(false);
    expect(shouldAutoApproveToolCall({ toolName: '', operation: 'read_file' }, {})).toBe(false);
  });

  it('never lets one enabled tool cover another tool sharing the same policy operation', () => {
    // 后端送审时 apply_diff / 定时任务增删 / MCP 客户端增删 / manage_secret set 全报 write_file。
    const rules = { write_file: true, read_file: true };

    expect(shouldAutoApproveToolCall({ toolName: 'write_file', operation: 'write_file' }, rules)).toBe(true);
    expect(shouldAutoApproveToolCall({ toolName: 'apply_diff', operation: 'write_file' }, rules)).toBe(false);
    expect(shouldAutoApproveToolCall({ toolName: 'add_mcp_client', operation: 'write_file' }, rules)).toBe(false);
    expect(shouldAutoApproveToolCall({ toolName: 'add_schedule', operation: 'write_file' }, rules)).toBe(false);
    expect(shouldAutoApproveToolCall({ toolName: 'manage_secret', operation: 'write_file' }, rules)).toBe(false);
    expect(shouldAutoApproveToolCall({ toolName: 'write_skill', operation: 'write_file' }, rules)).toBe(false);
    // read_file 勾着也不代表别的工具借它的 op 就能过。
    expect(shouldAutoApproveToolCall({ toolName: 'manage_secret', operation: 'read_file' }, rules)).toBe(false);
  });

  it('keeps a tool inside the operations its checkbox declares', () => {
    const rules = { search_in_files: true, list_dir: true, request_restart: true, execute_command: true };

    expect(shouldAutoApproveToolCall({ toolName: 'search_in_files', operation: 'read_file' }, rules)).toBe(true);
    // search_in_files 的 replace 模式报 write_file，勾只读的搜索不该顺带放行批量改写。
    expect(shouldAutoApproveToolCall({ toolName: 'search_in_files', operation: 'write_file' }, rules)).toBe(false);
    expect(shouldAutoApproveToolCall({ toolName: 'list_dir', operation: 'read_file' }, rules)).toBe(true);
    expect(shouldAutoApproveToolCall({ toolName: 'list_dir', operation: 'execute_command' }, rules)).toBe(false);
    expect(shouldAutoApproveToolCall({ toolName: 'request_restart', operation: 'restart' }, rules)).toBe(true);
  });

  it('requires an unlisted tool to have its borrowed operation enabled as well', () => {
    expect(shouldAutoApproveToolCall({ toolName: 'manage_secret', operation: 'write_file' }, { manage_secret: true })).toBe(false);
    expect(shouldAutoApproveToolCall(
      { toolName: 'manage_secret', operation: 'write_file' },
      { manage_secret: true, write_file: true },
    )).toBe(true);
    expect(shouldAutoApproveToolCall({ toolName: 'gemini_image', operation: 'gemini_image' }, { gemini_image: true })).toBe(true);
  });

  it('migrates operation-keyed rules without turning anything on', () => {
    // 旧版按 policy operation 匹配，list_dir / search_in_files / apply_diff / request_restart 那几个勾是空转的，
    // 直接按工具名启用就等于替用户放宽了。
    expect(migrateAutoApproveRules({ read_file: false, list_dir: true, search_in_files: true })).toMatchObject({
      read_file: false, list_dir: false, search_in_files: false,
    });
    // 用户压根没碰过 list_dir 也一样：它借的是 read_file，read_file 关了它就不能活过来。
    expect(migrateAutoApproveRules({ read_file: false })).toMatchObject({
      read_file: false, list_dir: false, search_in_files: false,
    });
    expect(migrateAutoApproveRules({ apply_diff: true, write_file: false })).toMatchObject({
      apply_diff: false, write_file: false,
    });
    expect(migrateAutoApproveRules({ apply_diff: true, write_file: true })).toMatchObject({
      apply_diff: true, write_file: true,
    });
    // request_restart 报的是 restart，旧规则里从来没放行过。
    expect(migrateAutoApproveRules({ request_restart: true })).toMatchObject({ request_restart: false });
    // 关着的永远不会被迁移成开着的。
    expect(migrateAutoApproveRules({ execute_command: false, unknown_tool: false })).toMatchObject({
      execute_command: false, unknown_tool: false,
    });
    // 默认那三项在默认状态下原样保留，迁移不该顺手把功能关光。
    expect(migrateAutoApproveRules({})).toMatchObject({
      read_file: true, list_dir: true, search_in_files: true, write_file: false, execute_command: false,
    });
  });

  it('migrates version 1 rules while hydrating the store', async () => {
    localStorage.setItem('clonoth_client_prefs', JSON.stringify({
      autoApproveTools: { read_file: false, list_dir: true, write_file: true, apply_diff: true },
      titleGeneration: 'manual',
    }));

    // store 是模块级单例，只有重新 import 才会再跑一次 localStorage 读取。
    vi.resetModules();
    const fresh = await import('../store/clientPrefsStore');
    const rules = fresh.useClientPrefsStore.getState().autoApproveTools;

    expect(rules.list_dir).toBe(false);
    expect(rules.read_file).toBe(false);
    expect(rules.search_in_files).toBe(false);
    expect(rules.write_file).toBe(true);
    expect(rules.apply_diff).toBe(true);
    expect(fresh.useClientPrefsStore.getState().titleGeneration).toBe('manual');

    // 迁移结果当场落盘并打上版本戳，下一版迁移不会再读到 v1 那份键。
    const persisted = JSON.parse(localStorage.getItem('clonoth_client_prefs') || '{}');
    expect(persisted.version).toBe(fresh.CLIENT_PREFS_VERSION);
    expect(persisted.autoApproveTools.list_dir).toBe(false);

    fresh.useClientPrefsStore.getState().setAutoApproveTool('read_file', true);
    const reread = JSON.parse(localStorage.getItem('clonoth_client_prefs') || '{}');
    // 打过版本戳之后不再二次迁移，否则用户刚勾上的又会被关掉。
    expect(reread.autoApproveTools.read_file).toBe(true);
  });

  it('leaves already-migrated rules untouched on reload', async () => {
    localStorage.setItem('clonoth_client_prefs', JSON.stringify({
      autoApproveTools: { read_file: false, list_dir: true },
      version: 2,
    }));

    vi.resetModules();
    const fresh = await import('../store/clientPrefsStore');

    expect(fresh.useClientPrefsStore.getState().autoApproveTools.list_dir).toBe(true);
  });

  it('persists changed auto-approval rules and title settings in localStorage', () => {
    useClientPrefsStore.getState().setAutoApproveTool('execute_command', true);
    useClientPrefsStore.getState().setTitleGeneration('manual');

    expect(useClientPrefsStore.getState().autoApproveTools.execute_command).toBe(true);
    expect(useClientPrefsStore.getState().titleGeneration).toBe('manual');
    expect(localStorage.getItem('clonoth_client_prefs')).toContain('execute_command');
  });

  it('renders client settings controls and updates preferences from the UI', () => {
    render(<ClientSettingsPage />);

    const titleSelect = screen.getByLabelText('对话标题生成方式');
    expect(titleSelect).toHaveValue('first-message');
    fireEvent.change(titleSelect, { target: { value: 'manual' } });
    expect(useClientPrefsStore.getState().titleGeneration).toBe('manual');

    const thinkingToggle = screen.getByLabelText('默认折叠思考内容');
    fireEvent.click(thinkingToggle);
    expect(useClientPrefsStore.getState().thinkingDefaultCollapsed).toBe(false);
  });

  it('no longer offers approval rules on the client page', () => {
    // 自动审批挪去了「工具与权限」，留在这里会让人以为它跟服务端策略是一回事。
    render(<ClientSettingsPage />);

    expect(screen.queryByLabelText('自动放行 execute_command')).not.toBeInTheDocument();
    expect(screen.queryByText('推荐工具')).not.toBeInTheDocument();
  });

  it('toggles an approval rule from the tools page section', async () => {
    render(<AutoApproveSection />);
    await screen.findByLabelText('搜索工具');

    const executeToggle = screen.getByLabelText('自动放行 execute_command');
    expect(executeToggle).not.toBeChecked();
    fireEvent.click(executeToggle);
    expect(useClientPrefsStore.getState().autoApproveTools.execute_command).toBe(true);
  });

  it('shows which policy operations each recommended rule covers', async () => {
    render(<AutoApproveSection />);
    await screen.findByLabelText('搜索工具');

    const applyDiffRow = screen.getByLabelText('自动放行 apply_diff').closest('label');
    expect(applyDiffRow).toHaveTextContent('覆盖操作');
    expect(applyDiffRow).toHaveTextContent('write_file');

    const restartRow = screen.getByLabelText('自动放行 request_restart').closest('label');
    expect(restartRow).toHaveTextContent('restart');

    expect(screen.getByText(/勾 write_file 不会连带放行它们/)).toBeInTheDocument();
  });

  it('lists every backend tool, not just the curated seven', async () => {
    render(<AutoApproveSection />);

    // 推荐那七个单独排在上面，剩下的全归「其他工具」。
    await screen.findByText(`其他工具 ${ALL_TOOL_NAMES.length - 7} 个`);
    fireEvent.click(screen.getByRole('button', { name: '展开' }));

    for (const toolName of ALL_TOOL_NAMES) {
      expect(screen.getByLabelText(`自动放行 ${toolName}`)).toBeInTheDocument();
    }
  });

  it('leaves every newly listed tool switched off', async () => {
    render(<AutoApproveSection />);

    await screen.findByRole('button', { name: '展开' });
    fireEvent.click(screen.getByRole('button', { name: '展开' }));

    // 用户之前根本没见过这些工具，列出来不等于放行。
    for (const toolName of ALL_TOOL_NAMES) {
      const box = screen.getByLabelText(`自动放行 ${toolName}`);
      if (['read_file', 'search_in_files', 'list_dir'].includes(toolName)) expect(box).toBeChecked();
      else expect(box).not.toBeChecked();
    }
    expect(screen.getByText('已放行 3 项')).toBeInTheDocument();
  });

  it('filters the full catalog from the search box', async () => {
    render(<AutoApproveSection />);
    await screen.findByLabelText('搜索工具');

    fireEvent.change(screen.getByLabelText('搜索工具'), { target: { value: 'schedule' } });

    expect(screen.getByLabelText('自动放行 create_schedule')).toBeInTheDocument();
    expect(screen.getByLabelText('自动放行 delete_schedule')).toBeInTheDocument();
    expect(screen.queryByLabelText('自动放行 gemini_image')).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('搜索工具'), { target: { value: 'nothing_matches' } });
    expect(screen.getByText(/没有名字匹配/)).toBeInTheDocument();
  });

  it('keeps enabled tools visible without expanding the catalog', async () => {
    useClientPrefsStore.getState().setAutoApproveTool('list_mcp_clients', true);
    render(<AutoApproveSection />);
    await screen.findByRole('button', { name: '展开' });

    // 列表是折着的，但已放行的必须一眼看得见，否则用户不知道自己开了什么。
    expect(screen.queryByLabelText('自动放行 list_mcp_clients')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消放行 list_mcp_clients' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '取消放行 list_mcp_clients' }));
    expect(useClientPrefsStore.getState().autoApproveTools.list_mcp_clients).toBe(false);
  });

  it('falls back to the known tools with an explicit notice when the catalog fails to load', async () => {
    vi.mocked(getAllToolNames).mockRejectedValue(new Error('401'));

    render(<AutoApproveSection />);

    await waitFor(() => expect(screen.getByText(/拉不到后端工具清单/)).toBeInTheDocument());
    // 退回已知项，不是一个都不显示 —— 空列表会让人以为自动审批被关了。
    expect(screen.getByText('推荐工具')).toBeInTheDocument();
    expect(screen.getByLabelText('自动放行 read_file')).toBeInTheDocument();
    expect(screen.getByLabelText('自动放行 request_restart')).toBeInTheDocument();
    expect(screen.queryByText(/^其他工具/)).not.toBeInTheDocument();
  });

  it('prefers an already loaded catalog over the failure fallback', async () => {
    vi.mocked(getAllToolNames).mockRejectedValue(new Error('offline'));
    useSettingsSelectionStore.setState({ allToolNames: ['read_file', 'gemini_image'] });

    render(<AutoApproveSection />);

    await screen.findByText('其他工具 1 个');
    expect(screen.queryByText(/拉不到后端工具清单/)).not.toBeInTheDocument();
  });
});
