import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import yaml from 'js-yaml';
import * as api from '../api/supervisorClient';
import { ApprovalCard } from '../components/chat/ApprovalCard';
import { ChatInput } from '../components/chat/ChatInput';
import { NodeFilesSettingsPage } from '../components/settings/pages/NodeFilesSettingsPage';
import { AgentsSettingsRightPanel, AutomationSettingsRightPanel } from '../components/settings/panels/SettingsContextPanels';
import { hasLikelyYamlSyntaxIssue } from '../components/settings/pages/settingsPagePrimitives';
import { parseMcpClients, parseNodeConfig, parseSchedules, serializeMcpClients, serializeNodeConfig, serializeSchedules } from '../components/settings/settingsStructuredConfig';
import { useConsoleStore } from '../console/consoleStore';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...await importOriginal<typeof import('../api/supervisorClient')>(),
  decideApproval: vi.fn(), getNodeFiles: vi.fn(), getNodeFileRaw: vi.fn(), updateNodeFileRaw: vi.fn(),
  getQqRaw: vi.fn(), getQqState: vi.fn(), updateQqRaw: vi.fn(),
  getNodeRaw: vi.fn(), updateNodeRaw: vi.fn(), getNodes: vi.fn(), getProviders: vi.fn(),
  getSchedulesRaw: vi.fn(), updateSchedulesRaw: vi.fn(),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'synthetic-token', isAuthenticated: true });
  useSettingsSelectionStore.setState({ selectedNode: null, selectedScheduleId: null });
  useConsoleStore.setState({ draft: {}, raw: '', live: null, saving: false, loading: false, error: '', notice: '' });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('composer and approval failure integrity', () => {
  it('keeps text and attachment on failed send, prevents duplicate sends, and clears only after success', async () => {
    const request = deferred<void>();
    const onSend = vi.fn().mockReturnValueOnce(request.promise).mockResolvedValueOnce(undefined);
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:synthetic'), revokeObjectURL: vi.fn() }));
    const { container } = render(<ChatInput conversationId="a" onSend={onSend} />);
    fireEvent.change(screen.getByLabelText('消息内容'), { target: { value: 'keep me' } });
    fireEvent.change(container.querySelector('input[type=file]')!, { target: { files: [new File(['test'], 'sample.txt')] } });
    fireEvent.submit(container.querySelector('form')!);
    fireEvent.submit(container.querySelector('form')!);
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('消息内容')).toBeDisabled();
    await act(async () => request.reject(new Error('offline')));
    expect(screen.getByLabelText('消息内容')).toHaveValue('keep me');
    expect(screen.getByText('sample.txt')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('offline');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(screen.getByLabelText('消息内容')).toHaveValue(''));
    expect(screen.queryByText('sample.txt')).not.toBeInTheDocument();
  });

  it('keeps separate drafts when a late send finishes in another conversation', async () => {
    const request = deferred<void>();
    const onSend = vi.fn(() => request.promise);
    const { rerender } = render(<ChatInput conversationId="a" onSend={onSend} />);
    fireEvent.change(screen.getByLabelText('消息内容'), { target: { value: 'A' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    rerender(<ChatInput conversationId="b" onSend={onSend} />);
    fireEvent.change(screen.getByLabelText('消息内容'), { target: { value: 'B' } });
    await act(async () => request.resolve());
    expect(screen.getByLabelText('消息内容')).toHaveValue('B');
    rerender(<ChatInput conversationId="a" onSend={onSend} />);
    expect(screen.getByLabelText('消息内容')).toHaveValue('');
  });

  it('moves an unsent new-conversation draft to the assigned id when sending fails', async () => {
    const request = deferred<void>();
    const onSend = vi.fn(() => request.promise);
    const { rerender } = render(<ChatInput conversationId="new" onSend={onSend} />);
    fireEvent.change(screen.getByLabelText('消息内容'), { target: { value: 'first message' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    rerender(<ChatInput conversationId="assigned" onSend={onSend} />);
    await act(async () => request.reject(new Error('upload failed')));
    expect(screen.getByLabelText('消息内容')).toHaveValue('first message');
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled();
  });

  it('does not turn a failed approval request into a denial and permits retry', async () => {
    const request = deferred<void>();
    vi.mocked(api.decideApproval).mockReturnValueOnce(request.promise).mockResolvedValueOnce(undefined);
    render(<ApprovalCard approval={{ id: 'approval', status: 'pending', operation: 'synthetic', details: {} }} />);
    fireEvent.click(screen.getByRole('button', { name: '允许' }));
    expect(screen.getByRole('button', { name: '拒绝' })).toBeDisabled();
    await act(async () => request.reject(new Error('HTTP 503')));
    expect(screen.queryByText('已拒绝')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('HTTP 503');
    fireEvent.click(screen.getByRole('button', { name: '允许' }));
    expect(await screen.findByText('已批准')).toBeInTheDocument();
  });
});

describe('configuration preservation', () => {
  it('keeps schedule ownership, unknown root fields and untouched rows across an edit', () => {
    const raw = 'future_root: {keep: true}\nschedules:\n  - id: a\n    cron: "0 0 * * *"\n    created_by: qq:owner\n    custom: {x: [1, 2]}\n  - id: b\n    cron: "0 1 * * *"\n    created_by: other\n';
    const rows = parseSchedules(raw); rows[0].text = 'changed';
    expect(yaml.load(serializeSchedules(rows, raw))).toMatchObject({ future_root: { keep: true }, schedules: [
      { id: 'a', created_by: 'qq:owner', custom: { x: [1, 2] }, text: 'changed' }, { id: 'b', created_by: 'other' },
    ] });
  });
  it('keeps unknown MCP fields while changing only the selected row', () => {
    const raw = 'future: x\nclients:\n  a:\n    transport: stdio\n    command: test\n    extra: {retry: 9}\n';
    const rows = parseMcpClients(raw); rows[0].description = 'edited';
    expect(yaml.load(serializeMcpClients(rows, raw))).toMatchObject({ future: 'x', clients: { a: { extra: { retry: 9 }, description: 'edited' } } });
  });
  it('roundtrips MCP arguments containing commas, newlines and significant whitespace', () => {
    const args = ['--python', 'print("a,b")', 'one\ntwo', ' leading space '];
    const raw = yaml.dump({ clients: { a: { transport: 'stdio', command: 'synthetic', args } } });
    const rows = parseMcpClients(raw); rows[0].description = 'edited';
    expect(yaml.load(serializeMcpClients(rows, raw))).toMatchObject({ clients: { a: { args } } });
    expect(() => serializeMcpClients([{ ...rows[0], argsText: '[42]' }], raw)).toThrow('字符串数组');
  });
  it.each(['schedules: text', 'schedules: {}', 'schedules: [null]', 'schedules: [{id: ok}, broken]', 'schedules: [{id: a, type: future}]', '- bad', 'schedules: ['])('refuses lossy schedule rewrite: %s', raw => {
    expect(() => parseSchedules(raw)).toThrow();
    expect(() => serializeSchedules([], raw)).toThrow();
  });
  it.each(['clients: []', 'clients: text', 'clients: {a: null}', 'clients: {a: {transport: stdio}, b: broken}', 'clients: {a: {transport: future}}'])('refuses lossy MCP rewrite: %s', raw => {
    expect(() => parseMcpClients(raw)).toThrow();
    expect(() => serializeMcpClients([], raw)).toThrow();
  });
  it('preserves structured node prompts instead of stringifying objects', () => {
    const raw = 'id: test\nprompt:\n  - role: system\n    content: hello\n';
    const form = parseNodeConfig(raw); form.name = 'renamed';
    expect(yaml.load(serializeNodeConfig(raw, form))).toMatchObject({ name: 'renamed', prompt: [{ role: 'system', content: 'hello' }] });
    expect(() => serializeNodeConfig(raw, { ...form, prompt: '{}' })).toThrow();
  });
  it('uses real YAML validation, allowing apostrophes in block text', () => {
    expect(hasLikelyYamlSyntaxIssue("prompt: |\n  It's valid\n")).toBe('');
    expect(hasLikelyYamlSyntaxIssue('prompt: [')).not.toBe('');
  });
});

describe('editor identity and draft protection', () => {
  it('does not enable saving stale node-file contents while the next file loads', async () => {
    const request = deferred<string>();
    vi.mocked(api.getNodeFiles).mockResolvedValue(['a.yaml', 'b.yaml'].map(name => ({ name, path: name, kind: 'node', size: 1, is_example: false, suffix: '.yaml', base_name: name, updated_at: 0 })));
    vi.mocked(api.getNodeFileRaw).mockImplementation(async (_, name) => name === 'a.yaml' ? 'id: a' : request.promise);
    render(<NodeFilesSettingsPage />);
    await waitFor(() => expect(screen.getByLabelText('文件内容')).toHaveValue('id: a'));
    fireEvent.change(screen.getByLabelText('当前文件'), { target: { value: 'b.yaml' } });
    expect(screen.getByLabelText('文件内容')).toHaveValue('');
    expect(screen.getByRole('button', { name: '保存当前文件' })).toBeDisabled();
    await act(async () => request.reject(new Error('cannot load b')));
    expect(screen.getByRole('button', { name: '保存当前文件' })).toBeDisabled();
    expect(api.updateNodeFileRaw).not.toHaveBeenCalled();
  });
  it('keeps the later selected node untouched by the previous node save', async () => {
    const request = deferred<void>();
    vi.mocked(api.getNodes).mockResolvedValue([]);
    vi.mocked(api.getProviders).mockResolvedValue({ registered: [], providers: {}, active_provider: '', fallbacks: [], node_fallbacks: {} });
    vi.mocked(api.getNodeRaw).mockImplementation(async (_, id) => `id: ${id}\nname: ${id}`);
    vi.mocked(api.updateNodeRaw).mockReturnValue(request.promise);
    useSettingsSelectionStore.setState({ selectedNode: { id: 'a', type: 'ai' } });
    render(<AgentsSettingsRightPanel />);
    await waitFor(() => expect(screen.getByLabelText('名称')).toHaveValue('a'));
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: 'edited A' } });
    fireEvent.click(screen.getByRole('button', { name: '保存节点配置' }));
    expect(screen.getByLabelText('名称')).toBeDisabled();
    act(() => useSettingsSelectionStore.getState().setSelectedNode({ id: 'b', type: 'ai' }));
    await waitFor(() => expect(screen.getByLabelText('名称')).toHaveValue('b'));
    await act(async () => request.resolve());
    expect(screen.getByLabelText('名称')).toHaveValue('b');
  });
  it('refuses a same-page schedule selection when discarding dirty fields is rejected', async () => {
    vi.mocked(api.getSchedulesRaw).mockResolvedValue('schedules:\n - id: a\n   text: original\n - id: b\n');
    useSettingsSelectionStore.setState({ selectedScheduleId: 'a' });
    render(<AutomationSettingsRightPanel />);
    const field = await screen.findByLabelText('text');
    fireEvent.change(field, { target: { value: 'unsaved' } });
    vi.mocked(window.confirm).mockReturnValue(false);
    act(() => useSettingsSelectionStore.getState().setSelectedScheduleId('b'));
    expect(useSettingsSelectionStore.getState().selectedScheduleId).toBe('a');
    expect(field).toHaveValue('unsaved');
  });
});

describe('shared QQ apply transaction', () => {
  const seed = () => useConsoleStore.setState({ live: { published: true, applied: true, file: { exists: true, size: 1, mtime_ns: 1 }, state: { values: { a: true, b: true }, paths: { a: 'signals.a', b: 'signals.b' } } } });
  it('preserves edits made while saving, including a revert to the formerly applied value', async () => {
    seed();
    const request = deferred<{ ok: boolean; warnings: string[] }>();
    let content = 'other: latest\n';
    vi.mocked(api.getQqRaw).mockImplementation(async () => ({ content, exists: true }));
    vi.mocked(api.updateQqRaw).mockImplementation(async (_, raw) => { content = raw; return request.promise; });
    vi.mocked(api.getQqState).mockRejectedValue(new Error('heartbeat unavailable'));
    useConsoleStore.getState().setDraft('a', false);
    const operation = useConsoleStore.getState().apply('synthetic');
    await waitFor(() => expect(api.updateQqRaw).toHaveBeenCalledOnce());
    useConsoleStore.getState().setDraft('a', true);
    useConsoleStore.getState().setDraft('b', false);
    request.resolve({ ok: true, warnings: [] });
    await operation;
    expect(useConsoleStore.getState().draft).toEqual({ a: true, b: false });
    expect(useConsoleStore.getState().raw).toContain('other: latest');
    expect(useConsoleStore.getState().live?.applied).toBe(false);
    expect(useConsoleStore.getState().notice).toContain('无法确认');
  });
  it('never PUTs over malformed YAML and keeps the draft for repair', async () => {
    seed();
    vi.mocked(api.getQqRaw).mockResolvedValue({ content: 'signals: [', exists: true });
    useConsoleStore.getState().setDraft('a', false);
    await useConsoleStore.getState().apply('synthetic');
    expect(api.updateQqRaw).not.toHaveBeenCalled();
    expect(useConsoleStore.getState().draft).toEqual({ a: false });
    expect(useConsoleStore.getState().error).not.toBe('');
  });
});
