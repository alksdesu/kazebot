import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AdvancedSettingsPage } from '../components/settings/pages/AdvancedSettingsPage';
import * as api from '../api/supervisorClient';
import { confirmNavigation } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getRuntimeFile: vi.fn(), resetRuntime: vi.fn(), getPolicyRaw: vi.fn(), updateRuntimeRaw: vi.fn(), updatePolicyRaw: vi.fn(),
}));
const runtime = '# keep this comment\nengine:\n  tool_mode: native\n  max_workers: 4\nshell:\n  entry_node_id: entry.original\ncustom:\n  preserved: true\n';
const runtimeFile = (content = runtime, source: api.RuntimeFile['source'] = 'default'): api.RuntimeFile => ({
  content, source, path: source === 'override' ? 'config/runtime.yaml' : 'defaults/runtime.yaml',
});
function deferred<T>() {
  let resolve!: (value: T) => void; let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function section(name: 'runtime' | 'policy') {
  return screen.getByText(name === 'runtime' ? '运行时配置 (runtime.yaml)' : '安全策略 (policy.yaml)').closest('details')!;
}
function openSection(name: 'runtime' | 'policy') {
  const element = section(name); element.open = true; fireEvent(element, new Event('toggle')); return within(element);
}
async function loadRuntime() {
  render(<AdvancedSettingsPage />); const area = openSection('runtime');
  fireEvent.click(area.getByRole('button', { name: '加载' }));
  await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.original'));
  return area;
}
const rawEditor = () => screen.getByLabelText('runtime.yaml YAML 编辑器');
const saveRuntime = () => fireEvent.click(within(section('runtime')).getByRole('button', { name: '保存运行时配置' }));

beforeEach(() => {
  vi.resetAllMocks(); vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'synthetic-auth', isAuthenticated: true });
  vi.mocked(api.getRuntimeFile).mockResolvedValue(runtimeFile()); vi.mocked(api.getPolicyRaw).mockResolvedValue('rules: []\n');
  vi.mocked(api.resetRuntime).mockResolvedValue({ ok: true, restored: true });
  vi.mocked(api.updateRuntimeRaw).mockResolvedValue({}); vi.mocked(api.updatePolicyRaw).mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('高级配置加载与提交边界', () => {
  it('按需加载，读入前及请求期间都不能编辑或重复读取', async () => {
    const read = deferred<api.RuntimeFile>(); vi.mocked(api.getRuntimeFile).mockReturnValue(read.promise);
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    expect(api.getRuntimeFile).not.toHaveBeenCalled(); expect(screen.getByLabelText('入口节点 ID')).toBeDisabled();
    expect(rawEditor()).toHaveAttribute('readonly');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    expect(screen.getByLabelText('工具调用格式')).toBeDisabled(); expect(rawEditor()).toBeDisabled();
    fireEvent.click(area.getByRole('button', { name: '正在加载…' })); expect(api.getRuntimeFile).toHaveBeenCalledTimes(1);
    await act(async () => read.resolve(runtimeFile()));
    expect(screen.getByLabelText('入口节点 ID')).toBeEnabled(); expect(rawEditor()).not.toHaveAttribute('readonly');
  });
  it.each(['engine: [', '- scalar-item\n', '   '])('坏原文 %j 不发生写入，错误被捕获且草稿保留', async invalid => {
    await loadRuntime(); fireEvent.change(rawEditor(), { target: { value: invalid } });
    fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.local' } });
    saveRuntime();
    await waitFor(() => expect(within(section('runtime')).getByRole('button', { name: '保存运行时配置' })).toBeEnabled());
    expect(api.updateRuntimeRaw).not.toHaveBeenCalled(); expect(rawEditor()).toHaveValue(invalid);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local');
    expect(within(section('runtime')).getAllByRole('status').some(node => node.textContent !== '有未保存修改')).toBe(true);
  });
  it('提交失败保留草稿，保存期间冻结表单和原文，重试成功后清理dirty', async () => {
    const write = deferred<unknown>(); vi.mocked(api.updateRuntimeRaw).mockReturnValueOnce(write.promise).mockResolvedValueOnce({});
    await loadRuntime(); fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.local' } });
    saveRuntime(); expect(screen.getByLabelText('入口节点 ID')).toBeDisabled(); expect(rawEditor()).toBeDisabled();
    await act(async () => write.reject(Error('写入失败'))); await screen.findByText('写入失败');
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local'); expect((rawEditor() as HTMLTextAreaElement).value).toContain('entry.local');
    saveRuntime(); await screen.findByText('已保存');
    expect(api.updateRuntimeRaw).toHaveBeenLastCalledWith('synthetic-auth', expect.stringContaining('# keep this comment'));
    expect(screen.queryByText('有未保存修改')).not.toBeInTheDocument(); vi.mocked(window.confirm).mockClear();
    expect(confirmNavigation()).toBe(true); expect(window.confirm).not.toHaveBeenCalled();
  });
  it('拒绝重新加载和导航时保留未保存草稿，读取失败也不清空它', async () => {
    const area = await loadRuntime(); fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.local' } });
    vi.mocked(window.confirm).mockReturnValue(false); fireEvent.click(area.getByRole('button', { name: '加载' }));
    expect(api.getRuntimeFile).toHaveBeenCalledTimes(1); expect(confirmNavigation()).toBe(false);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local');
    vi.mocked(window.confirm).mockReturnValue(true); vi.mocked(api.getRuntimeFile).mockRejectedValueOnce(Error('网络读取失败'));
    fireEvent.click(area.getByRole('button', { name: '加载' })); await screen.findByText('网络读取失败');
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local'); expect((rawEditor() as HTMLTextAreaElement).value).toContain('entry.local');
  });
  it('旧身份的加载结果不能覆盖新身份的配置', async () => {
    const old = deferred<api.RuntimeFile>(); vi.mocked(api.getRuntimeFile).mockImplementation(async token => token === 'synthetic-auth' ? old.promise : runtimeFile(runtime.replace('entry.original', 'entry.current')));
    render(<AdvancedSettingsPage />); let area = openSection('runtime'); fireEvent.click(area.getByRole('button', { name: '加载' }));
    await act(async () => useSettingsStore.setState({ adminToken: 'synthetic-new' }));
    area = within(section('runtime')); fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.current'));
    await act(async () => old.resolve(runtimeFile())); expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.current');
  });
});

describe('结构化与原文编辑一致性', () => {
  it('结构化草稿映射到原文，再编辑其他原文字段不会撤销结构化修改', async () => {
    await loadRuntime(); fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.local' } });
    const combined = (rawEditor() as HTMLTextAreaElement).value.replace('preserved: true', 'preserved: false');
    fireEvent.change(rawEditor(), { target: { value: combined } });
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local'); saveRuntime(); await screen.findByText('已保存');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('synthetic-auth', combined);
  });
  it('只编辑原文时保留原文格式，并同步结构化字段与保存基准', async () => {
    await loadRuntime(); const raw = '# manual formatting\nshell: {entry_node_id: entry.from_raw}\nengine: {tool_mode: json, max_workers: 8}\n';
    fireEvent.change(rawEditor(), { target: { value: raw } }); expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.from_raw');
    expect(screen.getByLabelText('工具调用格式')).toHaveValue('json'); saveRuntime(); await screen.findByText('已保存');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('synthetic-auth', raw); expect(rawEditor()).toHaveValue(raw);
    expect(screen.queryByText('有未保存修改')).not.toBeInTheDocument();
  });
  it('策略确认被拒绝不提交，网络失败保留原始策略草稿', async () => {
    render(<AdvancedSettingsPage />); const area = openSection('policy'); fireEvent.click(area.getByRole('button', { name: '加载' }));
    const editor = area.getByLabelText('policy.yaml YAML 编辑器'); await waitFor(() => expect(editor).toHaveValue('rules: []\n'));
    fireEvent.change(editor, { target: { value: 'rules: []\nnote: "user\'s choice"\n' } });
    vi.mocked(window.confirm).mockReturnValue(false); fireEvent.click(area.getByRole('button', { name: '保存策略 YAML' })); expect(api.updatePolicyRaw).not.toHaveBeenCalled();
    vi.mocked(window.confirm).mockReturnValue(true); vi.mocked(api.updatePolicyRaw).mockRejectedValueOnce(Error('策略保存失败'));
    fireEvent.click(area.getByRole('button', { name: '保存策略 YAML' })); await screen.findByText('策略保存失败');
    expect(editor).toHaveValue('rules: []\nnote: "user\'s choice"\n'); expect(editor).not.toHaveAttribute('readonly');
  });
});

describe('runtime 的来源与恢复默认', () => {
  const DEFAULT_HINT = '现在用的是仓库默认。保存后会在 config/runtime.yaml 存一份本实例的，没写到的键仍跟随仓库默认。';
  const OVERRIDE_HINT = '本实例有自己的 config/runtime.yaml，逐键盖在仓库默认上面；没写到的键跟随仓库更新。';
  const RESET_CONFIRM = '删掉本实例的 config/runtime.yaml，全部参数恢复成仓库默认？';
  const overriding = '# instance copy\nengine:\n  tool_mode: json\n  max_workers: 9\nshell:\n  entry_node_id: entry.override\n';
  const resetButton = () => within(section('runtime')).getByRole('button', { name: '恢复默认' });

  it('读回仓库默认时提示按默认那套说，恢复默认点不动', async () => {
    await loadRuntime();
    expect(screen.getByText(DEFAULT_HINT)).toBeInTheDocument();
    expect(screen.queryByText(OVERRIDE_HINT)).not.toBeInTheDocument();
    expect(resetButton()).toBeDisabled();
  });

  it('读回本实例那份时提示换一套说法，恢复默认可点', async () => {
    vi.mocked(api.getRuntimeFile).mockResolvedValue(runtimeFile(overriding, 'override'));
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override'));
    expect(screen.getByText(OVERRIDE_HINT)).toBeInTheDocument();
    expect(resetButton()).toBeEnabled();
  });

  it('加载之前不摆来源提示', () => {
    render(<AdvancedSettingsPage />); openSection('runtime');
    expect(screen.queryByText(DEFAULT_HINT)).not.toBeInTheDocument();
    expect(screen.queryByText(OVERRIDE_HINT)).not.toBeInTheDocument();
  });

  it('保存一次就从仓库默认变成本实例那份，恢复默认跟着可点', async () => {
    await loadRuntime();
    fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.local' } });
    saveRuntime(); await screen.findByText('已保存');
    expect(screen.getByText(OVERRIDE_HINT)).toBeInTheDocument();
    expect(resetButton()).toBeEnabled();
  });

  it('恢复默认删掉本实例那份并重读，表单回到仓库默认', async () => {
    vi.mocked(api.getRuntimeFile).mockResolvedValueOnce(runtimeFile(overriding, 'override')).mockResolvedValue(runtimeFile());
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override'));

    fireEvent.click(resetButton());
    await screen.findByText('已恢复成仓库默认');

    expect(window.confirm).toHaveBeenCalledWith(RESET_CONFIRM);
    expect(api.resetRuntime).toHaveBeenCalledWith('synthetic-auth');
    expect(api.getRuntimeFile).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.original');
    expect(rawEditor()).toHaveValue(runtime);
    expect(screen.getByText(DEFAULT_HINT)).toBeInTheDocument();
    expect(resetButton()).toBeDisabled();
    // 重读回来的那份就是新基准，恢复完不该还挂着未保存修改。
    expect(screen.queryByText('有未保存修改')).not.toBeInTheDocument();
  });

  it('未保存的草稿会一起丢掉，确认里先说清楚', async () => {
    vi.mocked(api.getRuntimeFile).mockResolvedValueOnce(runtimeFile(overriding, 'override')).mockResolvedValue(runtimeFile());
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override'));
    fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.draft' } });

    fireEvent.click(resetButton());
    await screen.findByText('已恢复成仓库默认');

    expect(window.confirm).toHaveBeenCalledWith(`${RESET_CONFIRM}未保存的修改也会一起丢掉。`);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.original');
  });

  it('确认被拒绝就什么都不删', async () => {
    vi.mocked(api.getRuntimeFile).mockResolvedValue(runtimeFile(overriding, 'override'));
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override'));

    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.click(resetButton());

    expect(api.resetRuntime).not.toHaveBeenCalled();
    expect(api.getRuntimeFile).toHaveBeenCalledTimes(1);
    expect(screen.getByText(OVERRIDE_HINT)).toBeInTheDocument();
  });

  it('恢复失败保留本实例那份，原因显示出来', async () => {
    vi.mocked(api.getRuntimeFile).mockResolvedValue(runtimeFile(overriding, 'override'));
    vi.mocked(api.resetRuntime).mockRejectedValueOnce(Error('409 这份文件不能删'));
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override'));

    fireEvent.click(resetButton());
    await screen.findByText('409 这份文件不能删');

    expect(api.getRuntimeFile).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override');
    expect(resetButton()).toBeEnabled();
  });
});
