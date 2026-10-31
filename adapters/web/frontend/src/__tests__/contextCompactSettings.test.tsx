import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import yaml from 'js-yaml';
import * as api from '../api/supervisorClient';
import { ContextCompactSettings } from '../console/ContextCompactSettings';
import { WHOLE_FILE_REWRITTEN } from '../console/yamlRewrite';
import { confirmNavigation } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async original => ({
  ...await original<typeof import('../api/supervisorClient')>(),
  getRuntimeFile: vi.fn(), getRuntimeLayers: vi.fn(), updateRuntimeRaw: vi.fn(),
}));

const COMMENTED = [
  '# 本实例的调参',
  'engine:',
  '  max_steps: 64            # 单轮最多几步',
  '',
  '  compact:',
  '    threshold_tokens: 256000   # 软阈值',
  '    keep_recent: 6             # 段数',
  '',
  'shell:',
  '  entry_node_id: bootstrap.shell_orchestrator',
  '',
].join('\n');

// 代码默认合上仓库默认的那一层：本实例没写的项跟着它
const DEFAULT_LAYER = {
  engine: { compact: { threshold_tokens: 256000, hard_threshold_tokens: 0, keep_recent_tokens: 30000, keep_recent: 6 } },
};

const layer = (content: string, hash: string | null = 'version-1'): api.RuntimeFile => ({ content, hash, source: 'override', path: 'config/runtime.yaml' });
const BUNDLED: api.RuntimeFile = { content: `# 仓库默认\n${yaml.dump(DEFAULT_LAYER)}`, hash: 'defaults-1', source: 'default', path: 'defaults/runtime.yaml' };

// 打开、重新载入读两层，保存前现读的是原文：两边都换成这一版。生效那层这块不看
function serve(file: api.RuntimeFile) {
  vi.mocked(api.getRuntimeFile).mockResolvedValue(file);
  vi.mocked(api.getRuntimeLayers).mockResolvedValue({ ...file, defaults: DEFAULT_LAYER, effective: DEFAULT_LAYER });
}

const input = (label: string) => screen.getByLabelText(label);
const soft = () => input('软阈值（token）');
const hard = () => input('硬阈值（token）');
const keepTokens = () => input('保留原文（token）');
const segments = () => input('保留段数');
const save = () => screen.getByRole('button', { name: '保存压缩设置' });
const reload = () => screen.getByRole('button', { name: '重新载入压缩设置' });
const change = (field: HTMLElement, value: string) => fireEvent.change(field, { target: { value } });
const written = () => vi.mocked(api.updateRuntimeRaw).mock.lastCall![1];

async function open() {
  render(<ContextCompactSettings />);
  await waitFor(() => expect(soft()).toBeEnabled());
}

async function saved() {
  fireEvent.click(save());
  await screen.findByText('压缩设置已保存。');
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { resolve, promise };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'compact-admin', isAuthenticated: true });
  serve(layer(COMMENTED));
  vi.mocked(api.updateRuntimeRaw).mockResolvedValue({ ok: true, hash: 'version-2' });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('读进来的样子', () => {
  // 还没有本实例那份时读到的是仓库默认：只写改的这一个键，不能把默认全文抄成本实例的一份。
  it('本实例没写的项留空，灰字是默认值；只改一项只落那一个键', async () => {
    serve(BUNDLED);
    await open();
    expect(soft()).toHaveValue('');
    expect(soft()).toHaveAttribute('placeholder', '默认 256000');
    expect(hard()).toHaveAttribute('placeholder', '默认 0');
    expect(keepTokens()).toHaveAttribute('placeholder', '默认 30000');
    expect(segments()).toHaveAttribute('placeholder', '默认 6');
    expect(save()).toBeDisabled();

    change(soft(), '120000');
    await saved();
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('compact-admin', 'engine:\n  compact:\n    threshold_tokens: 120000\n', 'defaults-1');
    expect(save()).toBeDisabled();
  });

  it('哪一层都没写的项，灰字说跟随默认', async () => {
    vi.mocked(api.getRuntimeLayers).mockResolvedValue({ ...layer('engine:\n  max_steps: 64\n'), defaults: {}, effective: {} });
    await open();
    expect(hard()).toHaveValue('');
    expect(hard()).toHaveAttribute('placeholder', '跟随默认');
  });

  it('本实例写了的项照实显示，没写的留空', async () => {
    await open();
    expect(soft()).toHaveValue('256000');
    expect(segments()).toHaveValue('6');
    expect(hard()).toHaveValue('');
    expect(keepTokens()).toHaveValue('');
  });

  it('改回原来的值不算改动', async () => {
    await open();
    change(soft(), '120000');
    expect(save()).toBeEnabled();
    change(soft(), '256000');
    expect(save()).toBeDisabled();
  });

  // engine 读这几项时按整数取，读不成就悄悄按默认跑：照实摆出来，人在这里就能改好。
  it.each([
    ['keep_recent: abc', 'abc'],
    ['keep_recent: null', 'null'],
    ['keep_recent: 100', '100'],
    ['keep_recent: 1.5', '1.5'],
  ])('本实例写成 %s：照实显示并指出来，改好才能存', async (line, shown) => {
    serve(layer(`engine:\n  compact:\n    threshold_tokens: 120000\n    ${line}\n`));
    await open();
    expect(segments()).toHaveValue(shown);
    expect(screen.getByRole('alert')).toHaveTextContent('保留段数须为 2～50 的整数');
    change(soft(), '90000');
    expect(save()).toBeDisabled();

    change(segments(), '');
    await saved();
    expect(written()).toBe('engine:\n  compact:\n    threshold_tokens: 90000\n');
  });

  it.each([
    ['缺版本指纹', layer(COMMENTED, null), '服务端未返回配置版本'],
    ['compact 写成了一个值', layer('engine:\n  compact: invalid\n'), 'engine.compact 不是一段配置'],
    ['engine 写成了列表', layer('engine:\n  - a\n'), 'engine 不是一段配置'],
    ['原文不是合法的 YAML', layer('engine: [\n'), '原文不是合法的 YAML'],
  ])('%s：不让在这里改', async (_name, file, message) => {
    serve(file);
    render(<ContextCompactSettings />);
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(soft()).toBeDisabled();
    expect(save()).toBeDisabled();
  });

  it('读取失败可以重试，不拿编出来的默认值给人存', async () => {
    vi.mocked(api.getRuntimeLayers).mockRejectedValueOnce(new api.ApiError(503, 'unavailable'));
    render(<ContextCompactSettings />);
    await screen.findByText('503 unavailable');
    expect(soft()).toBeDisabled();
    expect(save()).toBeDisabled();
    fireEvent.click(reload());
    await waitFor(() => expect(soft()).toHaveValue('256000'));
  });
});

describe('写回去的样子', () => {
  it('改一项只动原文那一行，注释和别的键照旧', async () => {
    await open();
    change(soft(), '120000');
    await saved();
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('compact-admin', COMMENTED.replace('threshold_tokens: 256000', 'threshold_tokens: 120000'), 'version-1');
  });

  it('一次改几项只写改过的那几项', async () => {
    await open();
    change(hard(), '320000');
    change(keepTokens(), '20000');
    await saved();
    expect(yaml.load(written())).toEqual({
      engine: { max_steps: 64, compact: { threshold_tokens: 256000, keep_recent: 6, hard_threshold_tokens: 320000, keep_recent_tokens: 20000 } },
      shell: { entry_node_id: 'bootstrap.shell_orchestrator' },
    });
    expect(written()).toContain('# 本实例的调参');
    expect(written()).toContain('threshold_tokens: 256000   # 软阈值');
  });

  it('清空一项就删掉那个键，之后跟随默认', async () => {
    await open();
    change(segments(), '');
    await saved();
    expect(yaml.load(written())).toEqual({
      engine: { max_steps: 64, compact: { threshold_tokens: 256000 } },
      shell: { entry_node_id: 'bootstrap.shell_orchestrator' },
    });
    expect(written()).toContain('threshold_tokens: 256000   # 软阈值');
    expect(segments()).toHaveValue('');
    expect(segments()).toHaveAttribute('placeholder', '默认 6');
    expect(save()).toBeDisabled();
  });

  it('0 照写进去，不当成留空', async () => {
    await open();
    change(soft(), '0');
    await saved();
    expect(yaml.load(written())).toMatchObject({ engine: { compact: { threshold_tokens: 0 } } });
  });

  it('本实例那份没有 compact 段时建出来，别的键照旧', async () => {
    serve(layer('engine:\n  max_steps: 64\n'));
    await open();
    change(hard(), '150000');
    await saved();
    expect(written()).toBe('engine:\n  max_steps: 64\n  compact:\n    hard_threshold_tokens: 150000\n');
  });

  it('前导零和首尾空格不带进去，存完输入框显示写进去的那个数', async () => {
    await open();
    change(keepTokens(), ' 020000 ');
    await saved();
    expect(yaml.load(written())).toMatchObject({ engine: { compact: { keep_recent_tokens: 20000 } } });
    expect(keepTokens()).toHaveValue('20000');
    expect(save()).toBeDisabled();
  });

  it.each([
    ['软阈值（token）', '-1', '软阈值须为不小于 0 的整数，留空跟随默认。'],
    ['软阈值（token）', '1.5', '软阈值须为不小于 0 的整数，留空跟随默认。'],
    ['硬阈值（token）', 'abc', '硬阈值须为不小于 0 的整数，留空跟随默认。'],
    ['硬阈值（token）', '9007199254740993', '硬阈值须为不小于 0 的整数，留空跟随默认。'],
    ['保留原文（token）', '1e5', '保留原文须为不小于 0 的整数，留空跟随默认。'],
    ['保留段数', '1', '保留段数须为 2～50 的整数，留空跟随默认。'],
    ['保留段数', '51', '保留段数须为 2～50 的整数，留空跟随默认。'],
  ])('%s 填 %s 存不了，输入原样留着', async (label, value, message) => {
    await open();
    change(input(label), value);
    expect(input(label)).toHaveValue(value);
    expect(save()).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent(message);
    expect(api.updateRuntimeRaw).not.toHaveBeenCalled();
  });

  it.each(['2', '50'])('保留段数的边界 %s 存得下', async value => {
    await open();
    change(segments(), value);
    await saved();
    expect(yaml.load(written())).toMatchObject({ engine: { compact: { keep_recent: Number(value) } } });
    expect(written()).toContain('# 段数');
  });

  it('原文只能整份重写、注释没保住时，结果里如实交代', async () => {
    serve(layer('# 会丢的注释\nengine: {compact: {threshold_tokens: 256000}}\n'));
    await open();
    change(soft(), '120000');
    fireEvent.click(save());
    expect(await screen.findByRole('status')).toHaveTextContent(`压缩设置已保存。${WHOLE_FILE_REWRITTEN}。`);
    expect(written()).toBe('engine:\n  compact:\n    threshold_tokens: 120000\n');
  });

  it('后端的提醒接在结果后面', async () => {
    vi.mocked(api.updateRuntimeRaw).mockResolvedValue({ ok: true, hash: 'version-2', warnings: ['engine.other 没有任何地方读'] });
    await open();
    change(soft(), '120000');
    fireEvent.click(save());
    expect(await screen.findByRole('status')).toHaveTextContent('压缩设置已保存。engine.other 没有任何地方读');
  });
});

describe('和别处同时改', () => {
  it('同一项打开之后被别处改过：草稿留着、不写入，重新载入要确认丢弃', async () => {
    await open();
    change(soft(), '120000');
    serve(layer(COMMENTED.replace('256000', '200000'), 'other-edit'));
    fireEvent.click(save());
    await screen.findByText(/压缩设置已被其他页面修改/);
    expect(soft()).toHaveValue('120000');
    expect(save()).toBeDisabled();
    expect(api.updateRuntimeRaw).not.toHaveBeenCalled();

    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.click(reload());
    expect(soft()).toHaveValue('120000');
    vi.mocked(window.confirm).mockReturnValue(true);
    fireEvent.click(reload());
    await waitFor(() => expect(soft()).toHaveValue('200000'));
    expect(save()).toBeDisabled();
  });

  it('别的键并发改了照常合并，用最新那版的指纹', async () => {
    await open();
    change(segments(), '8');
    const latest = layer(COMMENTED.replace('max_steps: 64', 'max_steps: 32'), 'concurrent-version');
    vi.mocked(api.getRuntimeFile).mockResolvedValue(latest);
    await saved();
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('compact-admin', latest.content.replace('keep_recent: 6', 'keep_recent: 8'), 'concurrent-version');
  });

  it('保存时撞上 409：草稿留着，挡住重复写', async () => {
    await open();
    change(soft(), '120000');
    vi.mocked(api.updateRuntimeRaw).mockRejectedValue(new api.ApiError(409, 'runtime.yaml 已被别处改过，刷新后再改'));
    fireEvent.click(save());
    await screen.findByText(/运行时配置在保存期间发生变化/);
    expect(soft()).toHaveValue('120000');
    expect(save()).toBeDisabled();
    fireEvent.click(save());
    expect(api.updateRuntimeRaw).toHaveBeenCalledTimes(1);
  });

  it('别的失败照实说，草稿留着，重试能存', async () => {
    await open();
    change(soft(), '120000');
    vi.mocked(api.updateRuntimeRaw).mockRejectedValueOnce(new api.ApiError(503, 'save failed'));
    fireEvent.click(save());
    await screen.findByText('503 save failed');
    expect(soft()).toHaveValue('120000');
    expect(save()).toBeEnabled();
    await saved();
  });

  // 写已经落盘了：报成失败，草稿留着，人会再存一次。下一次保存先现读最新一版再逐键核对，不靠这份回执。
  it('回执没读懂也按已保存收尾，原因另起一行，接着改还存得下去', async () => {
    await open();
    change(soft(), '120000');
    vi.mocked(api.updateRuntimeRaw).mockRejectedValueOnce(new api.ReceiptUnreadable('runtime.yaml 已经存上，可回执格式不对：缺 hash'));
    fireEvent.click(save());
    expect(await screen.findByRole('status')).toHaveTextContent('压缩设置已保存。');
    expect(screen.getByRole('alert')).toHaveTextContent('runtime.yaml 已经存上，可回执格式不对：缺 hash');
    expect(soft()).toHaveValue('120000');
    expect(save()).toBeDisabled();

    vi.mocked(api.getRuntimeFile).mockResolvedValue(layer(COMMENTED.replace('256000', '120000'), 'version-2'));
    change(soft(), '90000');
    await saved();
    expect(vi.mocked(api.updateRuntimeRaw).mock.lastCall![2]).toBe('version-2');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('连点保存只读一次、写一次', async () => {
    await open();
    change(soft(), '120000');
    const pending = deferred<api.RuntimeFile>();
    vi.mocked(api.getRuntimeFile).mockReturnValue(pending.promise);
    fireEvent.click(save());
    fireEvent.click(save());
    expect(api.getRuntimeLayers).toHaveBeenCalledTimes(1);
    expect(api.getRuntimeFile).toHaveBeenCalledTimes(1);
    expect(soft()).toBeDisabled();
    await act(async () => pending.resolve(layer(COMMENTED)));
    await screen.findByText('压缩设置已保存。');
    expect(api.updateRuntimeRaw).toHaveBeenCalledTimes(1);
  });

  it.each(['token', 'aba', 'unmount'])('保存前的读取迟到时 %s 变了，旧身份不写', async transition => {
    const view = render(<ContextCompactSettings />);
    await waitFor(() => expect(soft()).toHaveValue('256000'));
    change(soft(), '120000');
    const pending = deferred<api.RuntimeFile>();
    vi.mocked(api.getRuntimeFile).mockReturnValueOnce(pending.promise);
    fireEvent.click(save());
    await act(async () => {
      if (transition === 'unmount') view.unmount();
      else {
        useSettingsStore.setState({ adminToken: 'other-admin' });
        if (transition === 'aba') useSettingsStore.setState({ adminToken: 'compact-admin' });
      }
      pending.resolve(layer(COMMENTED));
      await Promise.resolve();
    });
    expect(api.updateRuntimeRaw).not.toHaveBeenCalled();
    if (transition !== 'unmount') await waitFor(() => expect(soft()).toHaveValue('256000'));
  });

  it('有没存的草稿时离开要确认，存完就不用了', async () => {
    await open();
    change(soft(), '120000');
    vi.mocked(window.confirm).mockReturnValue(false);
    expect(confirmNavigation()).toBe(false);
    expect(window.confirm).toHaveBeenCalledWith('上下文压缩设置尚未保存，确定丢弃草稿并继续吗？');

    vi.mocked(window.confirm).mockReturnValue(true);
    await saved();
    vi.mocked(window.confirm).mockClear();
    expect(confirmNavigation()).toBe(true);
    expect(window.confirm).not.toHaveBeenCalled();
  });
});
