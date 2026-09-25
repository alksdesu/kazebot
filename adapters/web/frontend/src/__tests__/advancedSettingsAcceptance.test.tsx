import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AdvancedSettingsPage } from '../components/settings/pages/AdvancedSettingsPage';
import * as api from '../api/supervisorClient';
import { confirmNavigation } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getRuntimeRaw: vi.fn(), getPolicyRaw: vi.fn(), updateRuntimeRaw: vi.fn(), updatePolicyRaw: vi.fn(),
}));
const runtime = '# keep this comment\nengine:\n  tool_mode: native\n  max_workers: 4\nshell:\n  entry_node_id: entry.original\ncustom:\n  preserved: true\n';
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
  vi.mocked(api.getRuntimeRaw).mockResolvedValue(runtime); vi.mocked(api.getPolicyRaw).mockResolvedValue('rules: []\n');
  vi.mocked(api.updateRuntimeRaw).mockResolvedValue({}); vi.mocked(api.updatePolicyRaw).mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('高级配置加载与提交边界', () => {
  it('按需加载，读入前及请求期间都不能编辑或重复读取', async () => {
    const read = deferred<string>(); vi.mocked(api.getRuntimeRaw).mockReturnValue(read.promise);
    render(<AdvancedSettingsPage />); const area = openSection('runtime');
    expect(api.getRuntimeRaw).not.toHaveBeenCalled(); expect(screen.getByLabelText('入口节点 ID')).toBeDisabled();
    expect(rawEditor()).toHaveAttribute('readonly');
    fireEvent.click(area.getByRole('button', { name: '加载' }));
    expect(screen.getByLabelText('工具调用格式')).toBeDisabled(); expect(rawEditor()).toBeDisabled();
    fireEvent.click(area.getByRole('button', { name: '处理中...' })); expect(api.getRuntimeRaw).toHaveBeenCalledTimes(1);
    await act(async () => read.resolve(runtime));
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
    expect(api.getRuntimeRaw).toHaveBeenCalledTimes(1); expect(confirmNavigation()).toBe(false);
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local');
    vi.mocked(window.confirm).mockReturnValue(true); vi.mocked(api.getRuntimeRaw).mockRejectedValueOnce(Error('网络读取失败'));
    fireEvent.click(area.getByRole('button', { name: '加载' })); await screen.findByText('网络读取失败');
    expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.local'); expect((rawEditor() as HTMLTextAreaElement).value).toContain('entry.local');
  });
  it('旧身份的加载结果不能覆盖新身份的配置', async () => {
    const old = deferred<string>(); vi.mocked(api.getRuntimeRaw).mockImplementation(async token => token === 'synthetic-auth' ? old.promise : runtime.replace('entry.original', 'entry.current'));
    render(<AdvancedSettingsPage />); let area = openSection('runtime'); fireEvent.click(area.getByRole('button', { name: '加载' }));
    await act(async () => useSettingsStore.setState({ adminToken: 'synthetic-new' }));
    area = within(section('runtime')); fireEvent.click(area.getByRole('button', { name: '加载' }));
    await waitFor(() => expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.current'));
    await act(async () => old.resolve(runtime)); expect(screen.getByLabelText('入口节点 ID')).toHaveValue('entry.current');
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
