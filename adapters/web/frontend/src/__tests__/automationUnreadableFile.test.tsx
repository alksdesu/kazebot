// 定时任务列表页：schedules.yaml 读不成（请求失败、文件写坏）时报出原因，不说「暂无定时任务。」让人照着空表新建；真没有时照常说。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import { AutomationSettingsPage } from '../components/settings/pages/AutomationSettingsPage';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...await importOriginal<typeof import('../api/supervisorClient')>(),
  getSchedulesRaw: vi.fn(),
}));

const READ_FAILURE = '503 读不到 schedules.yaml';

const EMPTY = '暂无定时任务。';

const loaded = () => waitFor(() => expect(screen.getByRole('button', { name: '刷新' })).toBeEnabled());

beforeEach(() => {
  vi.resetAllMocks();
  useSettingsStore.setState({ adminToken: 'synthetic', isAuthenticated: true });
  useSettingsSelectionStore.setState({ selectedScheduleId: null });
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('读失败时报出原因，不说暂无定时任务', async () => {
  vi.mocked(api.getSchedulesRaw).mockRejectedValue(new Error(READ_FAILURE));

  render(<AutomationSettingsPage />);

  expect(await screen.findByRole('alert')).toHaveTextContent(READ_FAILURE);
  await loaded();
  expect(screen.queryByText(EMPTY)).toBeNull();
});

it('文件写坏了也一样', async () => {
  vi.mocked(api.getSchedulesRaw).mockResolvedValue({ content: 'schedules: [\n', hash: 'schedules-1' });

  render(<AutomationSettingsPage />);

  expect(await screen.findByRole('alert')).toHaveTextContent('原文不是合法的 YAML');
  await loaded();
  expect(screen.queryByText(EMPTY)).toBeNull();
});

it('文件里真没有任务时照常说暂无', async () => {
  vi.mocked(api.getSchedulesRaw).mockResolvedValue({ content: 'schedules: []\n', hash: 'schedules-1' });

  render(<AutomationSettingsPage />);
  await loaded();

  expect(api.getSchedulesRaw).toHaveBeenCalled();
  expect(screen.getByText(EMPTY)).toBeInTheDocument();
  expect(screen.queryByRole('alert')).toBeNull();
});

it('修好之后刷新读回空列表，照常说暂无', async () => {
  vi.mocked(api.getSchedulesRaw)
    .mockRejectedValueOnce(new Error(READ_FAILURE))
    .mockResolvedValue({ content: 'schedules: []\n', hash: 'schedules-1' });
  render(<AutomationSettingsPage />);
  await screen.findByRole('alert');
  await loaded();

  fireEvent.click(screen.getByRole('button', { name: '刷新' }));

  expect(await screen.findByText(EMPTY)).toBeInTheDocument();
  expect(screen.queryByRole('alert')).toBeNull();
});
