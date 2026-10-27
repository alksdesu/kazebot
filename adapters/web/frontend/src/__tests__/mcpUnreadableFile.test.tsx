// MCP 列表页：mcp.yaml 写坏时列表读不成，报出后端给的原因，不能说「暂无 MCP Client。」让人照着空表新建；真没有时照常说。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { ApiError } from '../api/request';
import * as api from '../api/supervisorClient';
import { McpSettingsPage } from '../components/settings/pages/McpSettingsPage';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...await importOriginal<typeof import('../api/supervisorClient')>(),
  getMcpClients: vi.fn(),
}));

const UNREADABLE = 'config/mcp.yaml 用不了：YAML 解析失败。先修好这份文件再改';

const EMPTY = '暂无 MCP Client。';

const loaded = () => waitFor(() => expect(screen.getByRole('button', { name: '刷新 MCP' })).toBeEnabled());

beforeEach(() => {
  vi.resetAllMocks();
  useSettingsStore.setState({ adminToken: 'synthetic', isAuthenticated: true });
  useSettingsSelectionStore.setState({ selectedMcpClient: null });
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('文件写坏时报出后端给的原因，不说暂无 MCP Client', async () => {
  vi.mocked(api.getMcpClients).mockRejectedValue(new ApiError(409, UNREADABLE));

  render(<McpSettingsPage />);

  expect(await screen.findByRole('alert')).toHaveTextContent(UNREADABLE);
  await loaded();
  expect(screen.queryByText(EMPTY)).toBeNull();
});

it('文件里真没有 Client 时照常说暂无', async () => {
  vi.mocked(api.getMcpClients).mockResolvedValue([]);

  render(<McpSettingsPage />);
  await loaded();

  expect(api.getMcpClients).toHaveBeenCalled();
  expect(screen.getByText(EMPTY)).toBeInTheDocument();
  expect(screen.queryByRole('alert')).toBeNull();
});

it('修好之后刷新读回空列表，照常说暂无', async () => {
  vi.mocked(api.getMcpClients).mockRejectedValueOnce(new ApiError(409, UNREADABLE)).mockResolvedValue([]);
  render(<McpSettingsPage />);
  await screen.findByRole('alert');
  await loaded();

  fireEvent.click(screen.getByRole('button', { name: '刷新 MCP' }));

  expect(await screen.findByText(EMPTY)).toBeInTheDocument();
  expect(screen.queryByRole('alert')).toBeNull();
});
