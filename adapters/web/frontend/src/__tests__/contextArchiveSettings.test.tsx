import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api/supervisorClient';
import { ContextArchiveSettings } from '../console/ContextArchiveSettings';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async original => ({
  ...await original<typeof import('../api/supervisorClient')>(),
  getRuntimeRaw: vi.fn(), updateRuntimeRaw: vi.fn(),
}));

const raw = (days = 7, hash = 'version-1'): api.RawText => ({
  content: `# 保留注释\nengine:\n  other_setting: true\n  context_archive:\n    retention_days: ${days} # 天数\nproviders:\n  enabled: true\n`,
  hash,
});
const input = () => screen.getByLabelText('归档保留天数');
const save = () => screen.getByRole('button', { name: '保存归档设置' });
const reload = () => screen.getByRole('button', { name: '重新载入归档设置' });
async function open(days = '7') {
  render(<ContextArchiveSettings />);
  await waitFor(() => expect(input()).toHaveValue(days));
}
function change(value: string) { fireEvent.change(input(), { target: { value } }); }
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { resolve, promise };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'archive-admin', isAuthenticated: true });
  vi.mocked(api.getRuntimeRaw).mockResolvedValue(raw());
  vi.mocked(api.updateRuntimeRaw).mockResolvedValue({ ok: true, hash: 'version-2' });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('压缩上下文归档保留设置', () => {
  it('未配置字段时显示七天，说明消息时间和归档清理范围', async () => {
    vi.mocked(api.getRuntimeRaw).mockResolvedValue({ content: 'providers:\n  enabled: true\n', hash: '' });
    await open();
    expect(save()).toBeDisabled();
    expect(screen.getByText(/保留原始消息时间/)).toBeInTheDocument();
    expect(screen.getByText(/从每份归档创建时开始计算/)).toHaveTextContent('不删除当前会话或压缩摘要');
    change('14'); fireEvent.click(save());
    await screen.findByText('归档保留设置已保存。');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('archive-admin', 'providers:\n  enabled: true\nengine:\n  context_archive:\n    retention_days: 14\n', '');
  });

  it.each(['', '0', '-1', '1.5', '3651', 'abc', '1e2'])('无效输入 %s 不可保存且保留输入', async value => {
    await open(); change(value);
    expect(input()).toHaveValue(value);
    expect(save()).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('1～3650');
    expect(api.updateRuntimeRaw).not.toHaveBeenCalled();
  });

  it.each(['1', '3650'])('支持边界值 %s 并保留无关配置和注释', async value => {
    await open(); change(value); fireEvent.click(save());
    await screen.findByText('归档保留设置已保存。');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('archive-admin', raw(Number(value)).content, 'version-1');
    expect(save()).toBeDisabled();
  });

  it('只合并其他字段的并发改动，使用最新文件指纹', async () => {
    await open(); change('30');
    const latest = { content: raw().content.replace('other_setting: true', 'other_setting: false'), hash: 'concurrent-version' };
    vi.mocked(api.getRuntimeRaw).mockResolvedValue(latest);
    fireEvent.click(save()); await screen.findByText('归档保留设置已保存。');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('archive-admin', latest.content.replace('retention_days: 7', 'retention_days: 30'), 'concurrent-version');
  });

  it('同字段变化保留草稿，不写入；重新载入需要确认丢弃', async () => {
    await open(); change('30');
    vi.mocked(api.getRuntimeRaw).mockResolvedValue(raw(14, 'other-edit'));
    fireEvent.click(save()); await screen.findByText(/归档设置已被其他页面修改/);
    expect(input()).toHaveValue('30'); expect(save()).toBeDisabled();
    expect(api.updateRuntimeRaw).not.toHaveBeenCalled();
    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.click(reload()); expect(input()).toHaveValue('30');
    vi.mocked(window.confirm).mockReturnValue(true);
    fireEvent.click(reload()); await waitFor(() => expect(input()).toHaveValue('14'));
    expect(save()).toBeDisabled();
  });

  it('保存期间发生 409 保留草稿并阻止重复写入', async () => {
    await open(); change('21');
    vi.mocked(api.updateRuntimeRaw).mockRejectedValue(Error('409 conflict'));
    fireEvent.click(save()); await screen.findByText(/运行时配置在保存期间发生变化/);
    expect(input()).toHaveValue('21'); expect(save()).toBeDisabled();
    fireEvent.click(save()); expect(api.updateRuntimeRaw).toHaveBeenCalledTimes(1);
  });

  it('加载失败可重试，不提供伪造默认值保存', async () => {
    vi.mocked(api.getRuntimeRaw).mockRejectedValueOnce(Error('503 unavailable'));
    render(<ContextArchiveSettings />); await screen.findByText('503 unavailable');
    expect(input()).toBeDisabled(); expect(save()).toBeDisabled();
    fireEvent.click(reload()); await waitFor(() => expect(input()).toHaveValue('7'));
  });

  it.each([
    { content: raw().content, hash: null },
    { content: 'engine:\n  context_archive:\n    retention_days: 0\n', hash: 'bad' },
    { content: 'engine:\n  context_archive:\n    retention_days: null\n', hash: 'bad' },
    { content: 'engine:\n  context_archive: invalid\n', hash: 'bad' },
  ])('非法配置或缺少版本不启用编辑 %#', async value => {
    vi.mocked(api.getRuntimeRaw).mockResolvedValue(value);
    render(<ContextArchiveSettings />); await screen.findByRole('alert');
    expect(input()).toBeDisabled(); expect(save()).toBeDisabled();
  });

  it('保存失败保持草稿，重试可以保存', async () => {
    await open(); change('30');
    vi.mocked(api.updateRuntimeRaw).mockRejectedValueOnce(Error('503 save failed'));
    fireEvent.click(save()); await screen.findByText('503 save failed');
    expect(input()).toHaveValue('30'); expect(save()).toBeEnabled();
    fireEvent.click(save()); await screen.findByText('归档保留设置已保存。');
  });

  it('重复点击保存只执行一次读取和写入', async () => {
    await open(); change('30');
    const pending = deferred<api.RawText>();
    vi.mocked(api.getRuntimeRaw).mockReturnValue(pending.promise);
    fireEvent.click(save()); fireEvent.click(save());
    expect(api.getRuntimeRaw).toHaveBeenCalledTimes(2);
    expect(input()).toBeDisabled();
    await act(async () => pending.resolve(raw()));
    await screen.findByText('归档保留设置已保存。');
    expect(api.updateRuntimeRaw).toHaveBeenCalledTimes(1);
  });

  it.each(['token', 'aba', 'unmount'])('保存前读取迟到时 %s 变化阻止旧身份写入', async transition => {
    const view = render(<ContextArchiveSettings />);
    await waitFor(() => expect(input()).toHaveValue('7')); change('30');
    const pending = deferred<api.RawText>();
    vi.mocked(api.getRuntimeRaw).mockReturnValueOnce(pending.promise);
    fireEvent.click(save());
    await act(async () => {
      if (transition === 'unmount') view.unmount();
      else {
        useSettingsStore.setState({ adminToken: 'other-admin' });
        if (transition === 'aba') useSettingsStore.setState({ adminToken: 'archive-admin' });
      }
      pending.resolve(raw());
      await Promise.resolve();
    });
    expect(api.updateRuntimeRaw).not.toHaveBeenCalled();
    if (transition !== 'unmount') await waitFor(() => expect(input()).toHaveValue('7'));
  });

  it('保存回包迟到不影响新身份草稿', async () => {
    await open(); change('30');
    const pending = deferred<api.RawSaveResult>();
    vi.mocked(api.updateRuntimeRaw).mockReturnValue(pending.promise);
    fireEvent.click(save());
    await waitFor(() => expect(api.updateRuntimeRaw).toHaveBeenCalledTimes(1));
    act(() => useSettingsStore.setState({ adminToken: 'other-admin' }));
    await waitFor(() => expect(input()).toHaveValue('7')); change('60');
    await act(async () => pending.resolve({ ok: true, hash: 'old-response' }));
    expect(input()).toHaveValue('60');
    expect(screen.queryByText('归档保留设置已保存。')).not.toBeInTheDocument();
    expect(save()).toBeEnabled();
  });
});
