// 定时任务右栏的保存：只改这一条里动过的字段，别的条目和注释照旧；写之前重读，别处改了别的条目照样合进去，改了这一条就不写。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import { AutomationSettingsRightPanel } from '../components/settings/panels/SettingsContextPanels';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getSchedulesRaw: vi.fn(),
  updateSchedulesRaw: vi.fn(),
}));

const RAW = [
  'schedules:',
  '  - id: a',
  '    cron: "0 9 * * *"',
  '    text: original # 早安',
  '  - id: b',
  '    cron: "0 21 * * *"',
  '    text: night',
  '',
].join('\n');

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'schedule-token', isAuthenticated: true });
  useSettingsSelectionStore.setState({ selectedScheduleId: 'a' });
  vi.mocked(api.getSchedulesRaw).mockResolvedValue({ content: RAW, hash: 'schedules-1' });
  vi.mocked(api.updateSchedulesRaw).mockResolvedValue({ ok: true, hash: 'schedules-2' });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useSettingsSelectionStore.setState({ selectedScheduleId: null });
});

async function saveText(value: string) {
  render(<AutomationSettingsRightPanel />);
  const field = await screen.findByLabelText('text');
  await waitFor(() => expect(field).toHaveValue('original'));
  fireEvent.change(field, { target: { value } });
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
}

describe('定时任务右栏的保存', () => {
  it('只改这一条里动过的字段，注释和别的条目照旧，后端的提醒接在结果后面', async () => {
    vi.mocked(api.updateSchedulesRaw).mockResolvedValue({ ok: true, hash: 'schedules-2', warnings: ['第 3 条没有 id，调度时会跳过'] });

    await saveText('changed');

    await waitFor(() => expect(api.updateSchedulesRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateSchedulesRaw).mock.calls[0]).toEqual(['schedule-token', RAW.replace('text: original', 'text: changed'), 'schedules-1']);
    expect(await screen.findByText('定时任务已保存。第 3 条没有 id，调度时会跳过')).toBeInTheDocument();
  });

  it('写之前别处改了别的条目，这次的改动合进最新那份', async () => {
    const latest = RAW.replace('text: night', 'text: late night');
    vi.mocked(api.getSchedulesRaw).mockResolvedValueOnce({ content: RAW, hash: 'schedules-1' }).mockResolvedValue({ content: latest, hash: 'schedules-9' });

    await saveText('changed');

    await waitFor(() => expect(api.updateSchedulesRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateSchedulesRaw).mock.calls[0]).toEqual(['schedule-token', latest.replace('text: original', 'text: changed'), 'schedules-9']);
  });

  it('这一条在打开之后被别处改过就不写，说清要先刷新', async () => {
    vi.mocked(api.getSchedulesRaw)
      .mockResolvedValueOnce({ content: RAW, hash: 'schedules-1' })
      .mockResolvedValue({ content: RAW.replace('"0 9 * * *"', '"0 10 * * *"'), hash: 'schedules-9' });

    await saveText('changed');

    expect(await screen.findByText('这个定时任务打开之后被别处改过，点「刷新」读入最新内容再改。')).toBeInTheDocument();
    expect(api.updateSchedulesRaw).not.toHaveBeenCalled();
  });

  it('校验不过的 400 原样说出原因，表单留着', async () => {
    vi.mocked(api.updateSchedulesRaw).mockRejectedValue(new api.ApiError(400, '第 1 条的 cron 写错了：0 9 * *'));

    await saveText('changed');

    expect(await screen.findByText('400 第 1 条的 cron 写错了：0 9 * *')).toBeInTheDocument();
    expect(screen.getByLabelText('text')).toHaveValue('changed');
  });
});
