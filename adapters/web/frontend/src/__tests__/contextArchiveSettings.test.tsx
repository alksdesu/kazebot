import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import yaml from 'js-yaml';
import * as api from '../api/supervisorClient';
import { ContextArchiveSettings } from '../console/ContextArchiveSettings';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async original => ({
  ...await original<typeof import('../api/supervisorClient')>(),
  getRuntimeFile: vi.fn(), getRuntimeLayers: vi.fn(), updateRuntimeRaw: vi.fn(),
}));

const layer = (content: string, hash: string | null): api.RuntimeFile => ({ content, hash, source: 'override', path: 'config/runtime.yaml' });
const raw = (days = 7, hash = 'version-1'): api.RuntimeFile => layer(
  `# 保留注释\nengine:\n  other_setting: true\n  context_archive:\n    retention_days: ${days} # 天数\nproviders:\n  enabled: true\n`,
  hash,
);
const DEFAULTS: api.RuntimeFile = {
  content: '# 仓库默认\nengine:\n  max_workers: 4\n  context_archive:\n    retention_days: 7\nproviders:\n  enabled: true\n',
  hash: 'defaults-1',
  source: 'default',
  path: 'defaults/runtime.yaml',
};
const DEFAULT_LAYER = { engine: { context_archive: { retention_days: 7 } } };
// 页面按后端合并好的生效值显示；不另给时，原文里写的就是生效的
const layersOf = (
  file: api.RuntimeFile, effective = yaml.load(file.content) as Record<string, unknown>,
): api.RuntimeLayers => ({ ...file, defaults: DEFAULT_LAYER, effective });
// 打开、重新载入读两层，保存前现读的是原文：两边都换成这一版
function serve(file: api.RuntimeFile, effective?: Record<string, unknown>) {
  vi.mocked(api.getRuntimeFile).mockResolvedValue(file);
  vi.mocked(api.getRuntimeLayers).mockResolvedValue(layersOf(file, effective));
}
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
  serve(raw());
  vi.mocked(api.updateRuntimeRaw).mockResolvedValue({ ok: true, hash: 'version-2' });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('压缩上下文归档保留设置', () => {
  it('未配置字段时显示七天，说明消息时间和归档清理范围', async () => {
    serve(layer('providers:\n  enabled: true\n', 'layer-1'), { providers: { enabled: true }, ...DEFAULT_LAYER });
    await open();
    expect(save()).toBeDisabled();
    expect(screen.getByText(/本实例没设过，显示的是默认值/)).toBeInTheDocument();
    expect(screen.getByText(/保留原始消息时间/)).toBeInTheDocument();
    expect(screen.getByText(/从每份归档创建时开始计算/)).toHaveTextContent('不删除当前会话或压缩摘要');
    change('14'); fireEvent.click(save());
    await screen.findByText('归档保留设置已保存。');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('archive-admin', 'providers:\n  enabled: true\nengine:\n  context_archive:\n    retention_days: 14\n', 'layer-1');
  });

  // 还没有本实例那份时读到的是仓库默认：只写改的这一个键，不能把默认全文抄成本实例的一份。
  // 本实例那份没写保留天数时，生效的是默认那层的天数，不是页面自己记着的 7
  it('本实例有自己的 runtime.yaml 但没写天数时，显示默认那层实际给的天数', async () => {
    serve(layer('providers:\n  enabled: true\n', 'layer-1'), { providers: { enabled: true }, engine: { context_archive: { retention_days: 30 } } });
    await open('30');
    expect(screen.getByText(/本实例没设过，显示的是默认值/)).toBeInTheDocument();
    change('45');
    expect(screen.queryByText(/本实例没设过/)).toBeNull();
    fireEvent.click(save()); await screen.findByText('归档保留设置已保存。');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('archive-admin', 'providers:\n  enabled: true\nengine:\n  context_archive:\n    retention_days: 45\n', 'layer-1');
    expect(screen.queryByText(/本实例没设过/)).toBeNull();
  });

  it('本实例自己写了天数就不说跟随默认', async () => {
    await open();
    expect(screen.queryByText(/本实例没设过/)).toBeNull();
  });

  it('本实例还没有自己的 runtime.yaml 时，保存只落这一个键', async () => {
    serve(DEFAULTS);
    await open();
    change('30'); fireEvent.click(save());
    await screen.findByText('归档保留设置已保存。');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('archive-admin', 'engine:\n  context_archive:\n    retention_days: 30\n', 'defaults-1');
  });

  it('落成本实例那份之后再改，就在那一份上改', async () => {
    serve(DEFAULTS);
    await open();
    change('30'); fireEvent.click(save());
    await screen.findByText('归档保留设置已保存。');
    vi.mocked(api.getRuntimeFile).mockResolvedValue(layer('engine:\n  context_archive:\n    retention_days: 30\n', 'version-2'));
    vi.mocked(api.updateRuntimeRaw).mockResolvedValue({ ok: true, hash: 'version-3' });
    change('40'); fireEvent.click(save());
    await waitFor(() => expect(api.updateRuntimeRaw).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.updateRuntimeRaw).mock.calls[1]).toEqual(['archive-admin', 'engine:\n  context_archive:\n    retention_days: 40\n', 'version-2']);
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
    const latest = layer(raw().content.replace('other_setting: true', 'other_setting: false'), 'concurrent-version');
    vi.mocked(api.getRuntimeFile).mockResolvedValue(latest);
    fireEvent.click(save()); await screen.findByText('归档保留设置已保存。');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('archive-admin', latest.content.replace('retention_days: 7', 'retention_days: 30'), 'concurrent-version');
  });

  it('同字段变化保留草稿，不写入；重新载入需要确认丢弃', async () => {
    await open(); change('30');
    serve(raw(14, 'other-edit'));
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
    vi.mocked(api.updateRuntimeRaw).mockRejectedValue(new api.ApiError(409, 'runtime.yaml 已被别处改过，刷新后再改'));
    fireEvent.click(save()); await screen.findByText(/运行时配置在保存期间发生变化/);
    expect(input()).toHaveValue('21'); expect(save()).toBeDisabled();
    fireEvent.click(save()); expect(api.updateRuntimeRaw).toHaveBeenCalledTimes(1);
  });

  // 判冲突看状态码：原因里写着 409 的别的失败照实说，草稿还能再存。
  it('报错文字里带着 409 但不是 409 响应，不当成冲突', async () => {
    await open(); change('21');
    vi.mocked(api.updateRuntimeRaw).mockRejectedValueOnce(new api.ApiError(502, '上游返回 409'));
    fireEvent.click(save()); await screen.findByText('502 上游返回 409');
    expect(input()).toHaveValue('21'); expect(save()).toBeEnabled();
  });

  it('校验不过的 400 原样说出原因，草稿留着', async () => {
    await open(); change('21');
    vi.mocked(api.updateRuntimeRaw).mockRejectedValueOnce(new api.ApiError(400, 'engine.context_archive.retention_days 要写成整数'));
    fireEvent.click(save()); await screen.findByText('400 engine.context_archive.retention_days 要写成整数');
    expect(input()).toHaveValue('21'); expect(save()).toBeEnabled();
  });

  it('保存通过但带着提醒时，每条都接在结果后面', async () => {
    vi.mocked(api.updateRuntimeRaw).mockResolvedValue({ ok: true, hash: 'version-2', warnings: ['engine.other_setting 没有任何地方读'] });
    await open(); change('21'); fireEvent.click(save());
    expect(await screen.findByRole('status')).toHaveTextContent('归档保留设置已保存。engine.other_setting 没有任何地方读');
  });

  // 写已经落盘了：报成失败，草稿留着，人会再存一次。下一次保存先现读最新一版再逐键核对，不靠这份回执。
  it('回执没读懂也按已保存收尾，原因另起一行，接着改还存得下去', async () => {
    await open(); change('21');
    vi.mocked(api.updateRuntimeRaw).mockRejectedValueOnce(new api.ReceiptUnreadable('runtime.yaml 已经存上，可回执格式不对：缺 hash'));
    fireEvent.click(save());

    expect(await screen.findByRole('status')).toHaveTextContent('归档保留设置已保存。');
    expect(screen.getByRole('alert')).toHaveTextContent('runtime.yaml 已经存上，可回执格式不对：缺 hash');
    expect(input()).toHaveValue('21'); expect(save()).toBeDisabled();

    vi.mocked(api.getRuntimeFile).mockResolvedValue(raw(21, 'version-2'));
    change('22'); fireEvent.click(save());
    await waitFor(() => expect(api.updateRuntimeRaw).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.updateRuntimeRaw).mock.calls[1][2]).toBe('version-2');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('加载失败可重试，不提供伪造默认值保存', async () => {
    vi.mocked(api.getRuntimeLayers).mockRejectedValueOnce(new api.ApiError(503, 'unavailable'));
    render(<ContextArchiveSettings />); await screen.findByText('503 unavailable');
    expect(input()).toBeDisabled(); expect(save()).toBeDisabled();
    fireEvent.click(reload()); await waitFor(() => expect(input()).toHaveValue('7'));
  });

  it.each([
    layer(raw().content, null),
    layer('engine:\n  context_archive:\n    retention_days: 0\n', 'bad'),
    layer('engine:\n  context_archive:\n    retention_days: null\n', 'bad'),
    layer('engine:\n  context_archive: invalid\n', 'bad'),
  ])('非法配置或缺少版本不启用编辑 %#', async value => {
    serve(value);
    render(<ContextArchiveSettings />); await screen.findByRole('alert');
    expect(input()).toBeDisabled(); expect(save()).toBeDisabled();
  });

  it('保存失败保持草稿，重试可以保存', async () => {
    await open(); change('30');
    vi.mocked(api.updateRuntimeRaw).mockRejectedValueOnce(new api.ApiError(503, 'save failed'));
    fireEvent.click(save()); await screen.findByText('503 save failed');
    expect(input()).toHaveValue('30'); expect(save()).toBeEnabled();
    fireEvent.click(save()); await screen.findByText('归档保留设置已保存。');
  });

  it('重复点击保存只执行一次读取和写入', async () => {
    await open(); change('30');
    const pending = deferred<api.RuntimeFile>();
    vi.mocked(api.getRuntimeFile).mockReturnValue(pending.promise);
    fireEvent.click(save()); fireEvent.click(save());
    // 打开时读的是两层，保存前现读原文只读了一次
    expect(api.getRuntimeLayers).toHaveBeenCalledTimes(1);
    expect(api.getRuntimeFile).toHaveBeenCalledTimes(1);
    expect(input()).toBeDisabled();
    await act(async () => pending.resolve(raw()));
    await screen.findByText('归档保留设置已保存。');
    expect(api.updateRuntimeRaw).toHaveBeenCalledTimes(1);
  });

  it.each(['token', 'aba', 'unmount'])('保存前读取迟到时 %s 变化阻止旧身份写入', async transition => {
    const view = render(<ContextArchiveSettings />);
    await waitFor(() => expect(input()).toHaveValue('7')); change('30');
    const pending = deferred<api.RuntimeFile>();
    vi.mocked(api.getRuntimeFile).mockReturnValueOnce(pending.promise);
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
