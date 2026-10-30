import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AdvancedSettingsPage } from '../components/settings/pages/AdvancedSettingsPage';
import * as api from '../api/supervisorClient';
import { confirmNavigation } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';
import yaml from 'js-yaml';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getRuntimeLayers: vi.fn(), resetRuntime: vi.fn(), getPolicyRaw: vi.fn(), updateRuntimeRaw: vi.fn(), updatePolicyRaw: vi.fn(),
}));
const runtime = '# keep this comment\nengine:\n  tool_mode: native\n  max_workers: 4\nshell:\n  entry_node_id: entry.original\ncustom:\n  preserved: true\n';
// 默认那层就是 runtime 这份：本实例有没有自己那份，表单的占位都照它给
const runtimeFile = (
  content = runtime, source: api.RuntimeFile['source'] = 'override', hash: string | null = 'runtime-1',
): api.RuntimeLayers => ({
  content, hash, source, path: source === 'override' ? 'config/runtime.yaml' : 'defaults/runtime.yaml',
  defaults: yaml.load(runtime) as Record<string, unknown>,
  effective: yaml.load(content) as Record<string, unknown>,
} as api.RuntimeLayers);
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
  await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toBeEnabled());
  return area;
}
const rawEditor = () => screen.getByLabelText('runtime.yaml YAML 编辑器');
const runtimeSave = () => within(section('runtime')).getByRole('button', { name: '保存运行时配置' });
const saveRuntime = () => fireEvent.click(runtimeSave());

beforeEach(() => {
  vi.resetAllMocks(); vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'synthetic-auth', isAuthenticated: true });
  vi.mocked(api.getRuntimeLayers).mockResolvedValue(runtimeFile()); vi.mocked(api.getPolicyRaw).mockResolvedValue({ content: 'rules: []\n', hash: 'policy-1' });
  vi.mocked(api.resetRuntime).mockResolvedValue({ ok: true, restored: true });
  vi.mocked(api.updateRuntimeRaw).mockResolvedValue({ ok: true, hash: 'runtime-2' });
  vi.mocked(api.updatePolicyRaw).mockResolvedValue({ ok: true, hash: 'policy-2' });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('高级配置加载与提交边界', () => {
  it('按需加载，读入前及请求期间都不能编辑或重复读取', async () => {
    const read = deferred<api.RuntimeLayers>(); vi.mocked(api.getRuntimeLayers).mockReturnValue(read.promise);
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    expect(api.getRuntimeLayers).not.toHaveBeenCalled(); expect(screen.getByLabelText('入口节点 ID')).toBeDisabled();
    expect(rawEditor()).toHaveAttribute('readonly');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    expect(screen.getByLabelText('工具调用格式')).toBeDisabled(); expect(rawEditor()).toBeDisabled();
    fireEvent.click(area.getByRole('button', { name: '正在加载…' })); expect(api.getRuntimeLayers).toHaveBeenCalledTimes(1);
    await act(async () => read.resolve(runtimeFile()));
    expect(screen.getByLabelText('入口节点 ID')).toBeEnabled(); expect(rawEditor()).not.toHaveAttribute('readonly');
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.original');
  });
  it.each(['engine: [', '- scalar-item\n'])('原文 %j 解析不了：表单停用，保存被挡下，草稿保留', async invalid => {
    await loadRuntime(); fireEvent.change(rawEditor(), { target: { value: invalid } });
    expect(screen.getByLabelText('入口节点 ID')).toBeDisabled();
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.original');
    saveRuntime();
    expect(api.updateRuntimeRaw).not.toHaveBeenCalled(); expect(rawEditor()).toHaveValue(invalid);
    expect(within(section('runtime')).getAllByRole('alert').some(node => node.textContent?.startsWith('没有保存：原文解析不了。'))).toBe(true);
  });
  it('本实例这一层清空了就存不了，要回到仓库默认走「恢复默认」', async () => {
    await loadRuntime(); fireEvent.change(rawEditor(), { target: { value: '   ' } });
    expect(runtimeSave()).toBeDisabled();
    expect(api.updateRuntimeRaw).not.toHaveBeenCalled();
  });
  it('提交失败保留草稿，保存期间冻结表单和原文，重试成功后清理dirty', async () => {
    const write = deferred<api.RawSaveResult>();
    vi.mocked(api.updateRuntimeRaw).mockReturnValueOnce(write.promise).mockResolvedValueOnce({ ok: true, hash: 'runtime-2' });
    await loadRuntime(); fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.local' } });
    saveRuntime(); expect(screen.getByLabelText('入口节点 ID')).toBeDisabled(); expect(rawEditor()).toBeDisabled();
    await act(async () => write.reject(Error('写入失败'))); expect(await screen.findByText('写入失败')).toHaveAttribute('role', 'alert');
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local'); expect((rawEditor() as HTMLTextAreaElement).value).toContain('entry.local');
    saveRuntime(); await screen.findByText('已保存');
    expect(api.updateRuntimeRaw).toHaveBeenLastCalledWith('synthetic-auth', runtime.replace('entry.original', 'entry.local'), 'runtime-1');
    expect(screen.queryByText('有未保存修改')).not.toBeInTheDocument(); vi.mocked(window.confirm).mockClear();
    expect(confirmNavigation()).toBe(true); expect(window.confirm).not.toHaveBeenCalled();
  });
  it('后端校验不过的 400 把原因原样摆出来，草稿留着', async () => {
    vi.mocked(api.updateRuntimeRaw).mockRejectedValueOnce(new api.ApiError(400, 'engine 要写成映射'));
    await loadRuntime(); fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.local' } });
    saveRuntime();
    expect(await screen.findByText('400 engine 要写成映射')).toHaveAttribute('role', 'alert');
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local');
  });
  it('保存成功但后端有提醒时，每条都接在「已保存」后面', async () => {
    vi.mocked(api.updateRuntimeRaw).mockResolvedValueOnce({ ok: true, hash: 'runtime-2', warnings: ['engine.max_steps 已经没有代码读取，可以删掉'] });
    await loadRuntime(); fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.local' } });
    saveRuntime();
    expect(await screen.findByText('已保存。engine.max_steps 已经没有代码读取，可以删掉')).toHaveAttribute('role', 'status');
  });
  it('拒绝重新加载和导航时保留未保存草稿，读取失败也不清空它', async () => {
    const area = await loadRuntime(); fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.local' } });
    vi.mocked(window.confirm).mockReturnValue(false); fireEvent.click(area.getByRole('button', { name: '加载' }));
    expect(api.getRuntimeLayers).toHaveBeenCalledTimes(1); expect(confirmNavigation()).toBe(false);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local');
    vi.mocked(window.confirm).mockReturnValue(true); vi.mocked(api.getRuntimeLayers).mockRejectedValueOnce(Error('网络读取失败'));
    fireEvent.click(area.getByRole('button', { name: '加载' })); await screen.findByText('网络读取失败');
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local'); expect((rawEditor() as HTMLTextAreaElement).value).toContain('entry.local');
  });
  it('旧身份的加载结果不能覆盖新身份的配置', async () => {
    const old = deferred<api.RuntimeLayers>(); vi.mocked(api.getRuntimeLayers).mockImplementation(async token => token === 'synthetic-auth' ? old.promise : runtimeFile(runtime.replace('entry.original', 'entry.current')));
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
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('synthetic-auth', combined, 'runtime-1');
  });
  it('表单改一个字段只动原文里那一行，注释和别的键照旧', async () => {
    await loadRuntime(); fireEvent.change(screen.getByLabelText('并发 task 处理数，留空使用默认值'), { target: { value: '8' } });
    expect(rawEditor()).toHaveValue(runtime.replace('max_workers: 4', 'max_workers: 8'));
  });
  it('只编辑原文时保留原文格式，并同步结构化字段与保存基准', async () => {
    await loadRuntime(); const raw = '# manual formatting\nshell: {entry_node_id: entry.from_raw}\nengine: {tool_mode: json, max_workers: 8}\n';
    fireEvent.change(rawEditor(), { target: { value: raw } }); expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.from_raw');
    expect(screen.getByLabelText('工具调用格式')).toHaveValue('json'); saveRuntime(); await screen.findByText('已保存');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('synthetic-auth', raw, 'runtime-1'); expect(rawEditor()).toHaveValue(raw);
    expect(screen.queryByText('有未保存修改')).not.toBeInTheDocument();
  });
  it('工具调用格式可以退回跟随仓库默认，原文里那一行跟着删掉', async () => {
    await loadRuntime(); fireEvent.change(screen.getByLabelText('工具调用格式'), { target: { value: '' } });
    expect(rawEditor()).toHaveValue(runtime.replace('  tool_mode: native\n', ''));
  });
  it('保存撞上冲突时按文件名说清楚，草稿留着重做', async () => {
    vi.mocked(api.updatePolicyRaw).mockRejectedValueOnce(new api.ApiError(409, 'policy.yaml 已被别处改过，刷新后再改'));
    render(<AdvancedSettingsPage />); const area = openSection('policy'); fireEvent.click(area.getByRole('button', { name: '加载' }));
    const editor = area.getByLabelText('policy.yaml YAML 编辑器'); await waitFor(() => expect(editor).toHaveValue('rules: []\n'));
    fireEvent.change(editor, { target: { value: 'rules: [a]\n' } });
    fireEvent.click(area.getByRole('button', { name: '保存策略 YAML' }));

    expect(await screen.findByText('policy.yaml 在打开之后被别处改过，没有保存：点「加载」读入最新内容，再把改动重做一遍。')).toBeInTheDocument();
    expect(editor).toHaveValue('rules: [a]\n');
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
  const DEFAULT_HINT = '本实例还没有自己的 config/runtime.yaml，下面的原文是空的本实例层：只写要改的键，其余跟随仓库默认。保存后存成 config/runtime.yaml。';
  const OVERRIDE_HINT = '本实例有自己的 config/runtime.yaml，逐键盖在仓库默认上面；没写到的键跟随仓库更新。';
  const RESET_CONFIRM = '删掉本实例的 config/runtime.yaml，全部参数恢复成仓库默认？';
  const overriding = '# instance copy\nengine:\n  tool_mode: json\n  max_workers: 9\nshell:\n  entry_node_id: entry.override\n';
  const resetButton = () => within(section('runtime')).getByRole('button', { name: '恢复默认' });
  const usingDefaults = () => vi.mocked(api.getRuntimeLayers).mockResolvedValue(runtimeFile(runtime, 'default'));

  it('读回仓库默认时提示按默认那套说，恢复默认点不动', async () => {
    usingDefaults();
    await loadRuntime();
    expect(screen.getByText(DEFAULT_HINT)).toBeInTheDocument();
    expect(screen.queryByText(OVERRIDE_HINT)).not.toBeInTheDocument();
    expect(resetButton()).toBeDisabled();
  });

  // 以前原文编辑器摆的是仓库默认全文，改一个键就把整份默认抄成本实例那份，之后仓库改默认值这个实例就收不到了。
  it('仓库默认在用时，原文编辑的是空的本实例层，默认值只摆在一旁给人看', async () => {
    usingDefaults();
    await loadRuntime();
    expect(rawEditor()).toHaveValue('');
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('');
    expect(screen.getByLabelText('入口节点 ID')).toHaveAttribute('placeholder', '仓库默认 entry.original');
    expect(screen.getByLabelText('工具调用格式')).toHaveValue('');
    expect(screen.getByRole('option', { name: '跟随仓库默认（native）' })).toBeInTheDocument();
    expect(screen.getByLabelText('仓库默认 runtime.yaml')).toHaveValue(runtime);
    expect(runtimeSave()).toBeDisabled();
  });

  it('仓库默认在用时改一个键，存下去的只有那一个键', async () => {
    usingDefaults();
    await loadRuntime();
    fireEvent.change(screen.getByLabelText('并发 task 处理数，留空使用默认值'), { target: { value: '8' } });
    expect(rawEditor()).toHaveValue('engine:\n  max_workers: 8\n');
    saveRuntime(); await screen.findByText('已保存');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('synthetic-auth', 'engine:\n  max_workers: 8\n', 'runtime-1');
  });

  it('读回本实例那份时提示换一套说法，恢复默认可点', async () => {
    vi.mocked(api.getRuntimeLayers).mockResolvedValue(runtimeFile(overriding, 'override'));
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override'));
    expect(screen.getByText(OVERRIDE_HINT)).toBeInTheDocument();
    expect(screen.queryByLabelText('仓库默认 runtime.yaml')).not.toBeInTheDocument();
    expect(resetButton()).toBeEnabled();
  });

  // 有了本实例那份以后，没写的键照样要说得出跟着默认是多少，不能只剩一句「留空跟随仓库默认」
  it('有了本实例那份，没写的键照样提示默认那层的值', async () => {
    vi.mocked(api.getRuntimeLayers).mockResolvedValue(runtimeFile('# instance copy\nengine:\n  max_workers: 9\n', 'override'));
    await loadRuntime();
    expect(screen.getByText(OVERRIDE_HINT)).toBeInTheDocument();
    expect(screen.getByLabelText('并发 task 处理数，留空使用默认值')).toHaveValue(9);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('');
    expect(screen.getByLabelText('入口节点 ID')).toHaveAttribute('placeholder', '仓库默认 entry.original');
    expect(screen.getByRole('option', { name: '跟随仓库默认（native）' })).toBeInTheDocument();
    expect(screen.queryByLabelText('仓库默认 runtime.yaml')).not.toBeInTheDocument();
  });

  it('加载之前不摆来源提示', () => {
    render(<AdvancedSettingsPage />); openSection('runtime');
    expect(screen.queryByText(DEFAULT_HINT)).not.toBeInTheDocument();
    expect(screen.queryByText(OVERRIDE_HINT)).not.toBeInTheDocument();
  });

  it('保存一次就从仓库默认变成本实例那份，恢复默认跟着可点', async () => {
    usingDefaults();
    await loadRuntime();
    fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.local' } });
    saveRuntime(); await screen.findByText('已保存');
    expect(screen.getByText(OVERRIDE_HINT)).toBeInTheDocument();
    expect(resetButton()).toBeEnabled();
  });

  it('恢复默认删掉本实例那份并重读，表单回到跟随仓库默认', async () => {
    vi.mocked(api.getRuntimeLayers).mockResolvedValueOnce(runtimeFile(overriding, 'override')).mockResolvedValue(runtimeFile(runtime, 'default'));
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override'));

    fireEvent.click(resetButton());
    await screen.findByText('已恢复成仓库默认');

    expect(window.confirm).toHaveBeenCalledWith(RESET_CONFIRM);
    expect(api.resetRuntime).toHaveBeenCalledWith('synthetic-auth');
    expect(api.getRuntimeLayers).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('');
    expect(screen.getByLabelText('入口节点 ID')).toHaveAttribute('placeholder', '仓库默认 entry.original');
    expect(rawEditor()).toHaveValue('');
    expect(screen.getByText(DEFAULT_HINT)).toBeInTheDocument();
    expect(resetButton()).toBeDisabled();
    // 重读回来的那份就是新基准，恢复完不该还挂着未保存修改。
    expect(screen.queryByText('有未保存修改')).not.toBeInTheDocument();
  });

  it('未保存的草稿会一起丢掉，确认里先说清楚', async () => {
    vi.mocked(api.getRuntimeLayers).mockResolvedValueOnce(runtimeFile(overriding, 'override')).mockResolvedValue(runtimeFile(runtime, 'default'));
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override'));
    fireEvent.change(screen.getByLabelText('入口节点 ID'), { target: { value: 'entry.draft' } });

    fireEvent.click(resetButton());
    await screen.findByText('已恢复成仓库默认');

    expect(window.confirm).toHaveBeenCalledWith(`${RESET_CONFIRM}未保存的修改也会一起丢掉。`);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('');
  });

  it('确认被拒绝就什么都不删', async () => {
    vi.mocked(api.getRuntimeLayers).mockResolvedValue(runtimeFile(overriding, 'override'));
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override'));

    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.click(resetButton());

    expect(api.resetRuntime).not.toHaveBeenCalled();
    expect(api.getRuntimeLayers).toHaveBeenCalledTimes(1);
    expect(screen.getByText(OVERRIDE_HINT)).toBeInTheDocument();
  });

  it('恢复失败保留本实例那份，原因显示出来', async () => {
    vi.mocked(api.getRuntimeLayers).mockResolvedValue(runtimeFile(overriding, 'override'));
    vi.mocked(api.resetRuntime).mockRejectedValueOnce(Error('409 这份文件不能删'));
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override'));

    fireEvent.click(resetButton());
    await screen.findByText('409 这份文件不能删');

    expect(api.getRuntimeLayers).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.override');
    expect(resetButton()).toBeEnabled();
  });
});
