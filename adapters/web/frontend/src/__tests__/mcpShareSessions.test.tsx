// MCP 编辑器的 share_sessions 复选框：照文件显示，勾上写 true，取消勾选删掉这个键，别的键一个不动；新建的 Client 不写它。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import yaml from 'js-yaml';

import * as api from '../api/supervisorClient';
import { McpSettingsPage } from '../components/settings/pages/McpSettingsPage';
import { McpSettingsRightPanel } from '../components/settings/panels/SettingsContextPanels';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...await importOriginal<typeof import('../api/supervisorClient')>(),
  getMcpClients: vi.fn(), getMcpClientsRaw: vi.fn(), updateMcpClientsRaw: vi.fn(),
}));

const CHECKBOX = 'share_sessions：所有对话共用会话';

type McpFile = { clients: Record<string, Record<string, unknown>> };

const serve = (alpha: Record<string, unknown>) => {
  vi.mocked(api.getMcpClientsRaw).mockResolvedValue({
    content: yaml.dump({ version: 1, clients: { alpha, beta: { transport: 'stdio', command: 'other' } } }),
    hash: 'mcp-1',
  });
  vi.mocked(api.updateMcpClientsRaw).mockResolvedValue({ ok: true, hash: 'mcp-2' });
};

const openEditor = async (alpha: Record<string, unknown>): Promise<HTMLInputElement> => {
  serve(alpha);
  useSettingsSelectionStore.setState({ selectedMcpClient: { id: 'alpha', transport: 'stdio', enabled: true } });
  render(<McpSettingsRightPanel />);
  return await screen.findByLabelText(CHECKBOX) as HTMLInputElement;
};

const written = async (): Promise<McpFile> => {
  await waitFor(() => expect(api.updateMcpClientsRaw).toHaveBeenCalledOnce());
  return yaml.load(vi.mocked(api.updateMcpClientsRaw).mock.calls[0][1]) as McpFile;
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'synthetic', isAuthenticated: true });
  useSettingsSelectionStore.setState({
    selectedNode: null, selectedTool: null, selectedSkill: null, selectedScheduleId: null, selectedMcpClient: null,
  });
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('shows what the file says and explains what sharing does to stateful servers', async () => {
  const box = await openEditor({ transport: 'stdio', command: 'run-me', share_sessions: 'yes' });

  expect(box).toBeChecked();
  expect(box).toHaveAccessibleDescription(/有状态的服务/);
});

it('writes share_sessions: true when ticked and leaves every other key as it was', async () => {
  const box = await openEditor({ transport: 'stdio', command: 'run-me', restart_hint: 'weekly' });
  expect(box).not.toBeChecked();

  fireEvent.click(box);
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  const { clients } = await written();

  expect(clients.alpha).toEqual({ transport: 'stdio', command: 'run-me', restart_hint: 'weekly', share_sessions: true });
  expect(clients.beta).toEqual({ transport: 'stdio', command: 'other' });
});

it('removes the key when unticked instead of writing false', async () => {
  const box = await openEditor({ transport: 'stdio', command: 'run-me', share_sessions: true, restart_hint: 'weekly' });
  expect(box).toBeChecked();

  fireEvent.click(box);
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  const { clients } = await written();

  expect(clients.alpha).toEqual({ transport: 'stdio', command: 'run-me', restart_hint: 'weekly' });
});

it('creates a client from the list page without share_sessions', async () => {
  serve({ transport: 'stdio', command: 'run-me' });
  vi.mocked(api.getMcpClients).mockResolvedValue([{ id: 'alpha', enabled: true, transport: 'stdio', command: 'run-me' }]);
  render(<McpSettingsPage />);
  await screen.findByRole('button', { name: /alpha/ });

  fireEvent.change(screen.getByLabelText('创建 MCP Client'), { target: { value: 'fresh' } });
  await waitFor(() => expect(screen.getByRole('button', { name: '创建 Client' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '创建 Client' }));
  const { clients } = await written();

  expect(clients.fresh).toEqual({
    transport: 'streamable_http', enabled: false, description: '通过设置页面创建的 MCP Client。', url: '', headers: {},
  });
});
