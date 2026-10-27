// 客户端设置页的入口节点：节点表读不到时说出来、给重试，不当成一个节点都没有；替人挑入口时优先挑后端配的默认入口，
// 全局配置没读到就不挑，存着的入口还在根入口里就不动。
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import type { AdminNode } from '../api/supervisorClient';
import { ClientSettingsPage } from '../components/settings/pages/ClientSettingsPage';
import { LOADED, useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getAppConfig: vi.fn(),
  getNodes: vi.fn(),
}));

const NODES: AdminNode[] = [
  { id: 'bootstrap.cmd_reviewer', type: 'ai', name: 'Reviewer' },
  { id: 'bootstrap.shell_orchestrator', type: 'ai', name: 'Shell', delegate_targets: ['bootstrap.cmd_reviewer'] },
  { id: 'bootstrap.coder', type: 'ai', name: 'Coder' },
];

const config = (defaultEntry: string) => ({ provider: 'openai', openai: { model: 'global-model', base_url: '' }, default_entry_node_id: defaultEntry });

const entrySelect = () => screen.getByLabelText('入口节点') as HTMLSelectElement;
const optionTexts = () => within(entrySelect()).getAllByRole('option').map((option) => option.textContent);

beforeEach(() => {
  localStorage.clear();
  useSettingsStore.getState().clearLoginData();
  useSettingsStore.setState({ adminToken: 'test-token', isAuthenticated: true, entryNodeId: '' });
  vi.mocked(api.getNodes).mockReset().mockResolvedValue(structuredClone(NODES));
  vi.mocked(api.getAppConfig).mockReset().mockResolvedValue(config('bootstrap.coder'));
});

describe('客户端设置页的入口节点', () => {
  it('节点表读不到时说出来，不替人挑入口；重试读到了挑后端配的默认入口', async () => {
    vi.mocked(api.getNodes).mockRejectedValueOnce(new Error('nodes 503'));
    render(<ClientSettingsPage />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('入口节点清单读取失败：nodes 503');
    expect(useSettingsStore.getState().entryNodeId).toBe('');
    expect(optionTexts()).toEqual(['（未设置）']);

    fireEvent.click(within(alert).getByRole('button', { name: '重试' }));

    await waitFor(() => expect(useSettingsStore.getState().entryNodeId).toBe('bootstrap.coder'));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(entrySelect()).toHaveValue('bootstrap.coder');
    expect(optionTexts()).toEqual(['Shell', 'Coder']);
  });

  it('后端配的默认入口不在根入口里时挑第一个根入口', async () => {
    vi.mocked(api.getAppConfig).mockResolvedValue(config('bootstrap.cmd_reviewer'));
    render(<ClientSettingsPage />);

    await waitFor(() => expect(useSettingsStore.getState().entryNodeId).toBe('bootstrap.shell_orchestrator'));
    expect(entrySelect()).toHaveValue('bootstrap.shell_orchestrator');
  });

  it('存着的入口还在根入口里就不动，哪怕后端配的默认入口是别的', async () => {
    useSettingsStore.setState({ entryNodeId: 'bootstrap.shell_orchestrator' });
    render(<ClientSettingsPage />);

    await waitFor(() => expect(useSettingsStore.getState()).toMatchObject({ nodeListRead: LOADED, globalConfigRead: LOADED }));
    // 挑入口在两样都读完以后才跑，等它跑完再看
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(useSettingsStore.getState().entryNodeId).toBe('bootstrap.shell_orchestrator');
    expect(entrySelect()).toHaveValue('bootstrap.shell_orchestrator');
  });

  it('存着的入口不在根入口里时，两样都读成了就换成后端配的默认入口', async () => {
    useSettingsStore.setState({ entryNodeId: 'gone.node' });
    render(<ClientSettingsPage />);

    await waitFor(() => expect(useSettingsStore.getState().entryNodeId).toBe('bootstrap.coder'));
    expect(within(entrySelect()).queryByRole('option', { name: /gone\.node/ })).toBeNull();
  });

  it('全局配置没读到时不替人挑：存着的入口照实摆出来，说默认入口没读到，重试读到了再挑', async () => {
    useSettingsStore.setState({ entryNodeId: 'gone.node' });
    vi.mocked(api.getAppConfig).mockRejectedValueOnce(new Error('config 500'));
    render(<ClientSettingsPage />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('后端默认入口读取失败：config 500');
    expect(within(entrySelect()).getByRole('option', { name: 'gone.node（不在入口节点列表里）' })).toBeInTheDocument();
    expect(entrySelect()).toHaveValue('gone.node');
    expect(useSettingsStore.getState().entryNodeId).toBe('gone.node');

    fireEvent.click(within(alert).getByRole('button', { name: '重试' }));

    await waitFor(() => expect(useSettingsStore.getState().entryNodeId).toBe('bootstrap.coder'));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('还在读时说正在加载', () => {
    vi.mocked(api.getNodes).mockReturnValue(new Promise(() => {}));
    render(<ClientSettingsPage />);

    expect(optionTexts()).toEqual(['正在加载…']);
  });

  it('读成了但一个入口都没有时说未设置，不说正在加载', async () => {
    vi.mocked(api.getNodes).mockResolvedValue([{ id: 'system.compactor', type: 'ai' }]);
    render(<ClientSettingsPage />);

    await waitFor(() => expect(useSettingsStore.getState().nodeListRead).toBe(LOADED));
    expect(optionTexts()).toEqual(['（未设置）']);
    expect(useSettingsStore.getState().entryNodeId).toBe('');
  });

  it('节点表读成过以后再读不成就沿用那一份，不报失败', async () => {
    useSettingsStore.getState().setNodeList(structuredClone(NODES));
    useSettingsStore.setState({ entryNodeId: 'bootstrap.coder' });
    vi.mocked(api.getNodes).mockRejectedValue(new Error('nodes 503'));
    const original = useSettingsStore.getState().failNodeList;
    const failNodeList = vi.fn(original);
    useSettingsStore.setState({ failNodeList });

    try {
      render(<ClientSettingsPage />);

      // 等这次重读真的没读成，再看页面有没有把它当成失败
      await waitFor(() => expect(failNodeList).toHaveBeenCalledWith('nodes 503'));
      expect(screen.queryByRole('alert')).toBeNull();
      expect(entrySelect()).toHaveValue('bootstrap.coder');
      expect(optionTexts()).toEqual(['Shell', 'Coder']);
    } finally {
      useSettingsStore.setState({ failNodeList: original });
    }
  });

  it('重读不成时按读成过的那份替人挑：存着的入口不在根入口里照样换成后端配的默认入口', async () => {
    useSettingsStore.getState().setNodeList(structuredClone(NODES));
    useSettingsStore.getState().setGlobalConfig('global-model', '', 'bootstrap.coder');
    useSettingsStore.setState({ entryNodeId: 'gone.node' });
    vi.mocked(api.getNodes).mockRejectedValue(new Error('nodes 503'));
    vi.mocked(api.getAppConfig).mockRejectedValue(new Error('config 500'));
    render(<ClientSettingsPage />);

    await waitFor(() => expect(useSettingsStore.getState().entryNodeId).toBe('bootstrap.coder'));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(optionTexts()).toEqual(['Shell', 'Coder']);
  });
});
