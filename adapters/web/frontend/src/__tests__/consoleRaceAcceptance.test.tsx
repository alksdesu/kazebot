import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api/supervisorClient';
import { ModelsPage } from '../console/ModelsPage';
import { ProvidersPage } from '../console/ProvidersPage';
import { MemoryPage } from '../console/MemoryPage';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getProviderProfiles: vi.fn(), getProviders: vi.fn(), getNodes: vi.fn(), getRuntimeRaw: vi.fn(), getNodeRaw: vi.fn(),
  updateRuntimeRaw: vi.fn(), updateNodeRaw: vi.fn(), upsertProvider: vi.fn(), setActiveProvider: vi.fn(), deleteProvider: vi.fn(),
  getMemoryOverview: vi.fn(), getMemoryEntries: vi.fn(), saveMemoryEntry: vi.fn(), deleteMemoryEntry: vi.fn(), clearMemoryNamespace: vi.fn(),
  getConversations: vi.fn(), getConversationMessages: vi.fn(), resetConversationBySession: vi.fn(), getQqScope: vi.fn(), migrateQqScope: vi.fn(),
}));
vi.mock('../console/FallbackChain', () => ({ FallbackChain: () => null }));
vi.mock('../console/SystemSlots', () => ({ SystemSlots: () => null }));
vi.mock('../console/VisionRouting', () => ({ VisionRouting: () => null }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const providerBlock = (name: string): api.ProviderConfigPublic => ({
  model: `${name}-model`, model_raw: `${name}-model`, base_url: '', base_url_raw: '', api_key_present: false,
  api_key_redacted: '', supports_vision: null, type: 'openai', type_explicit: true, label: '',
});
const providers = (): api.ProvidersResponse => ({
  active_provider: 'relay', registered: ['openai', 'anthropic'],
  providers: { relay: providerBlock('relay'), spare: providerBlock('spare') }, fallbacks: [], node_fallbacks: {},
});
const profiles: api.ProviderProfiles = {
  options: {
    openai: [{ key: 'temperature', label: '采样温度', kind: 'float', default: 1 }],
    anthropic: [{ key: 'temperature', label: '另一格式温度', kind: 'float', default: 1 }],
  }, wireFormats: { openai: 'openai', anthropic: 'anthropic' }, hostProfiles: {}, defaultVision: {},
};
const runtime = 'providers:\n  openai:\n    options:\n      temperature: 0.2\n  anthropic:\n    options:\n      temperature: 0.9\n';
const nodeRaw = (id: string, temperature: number) => `id: ${id}\nprovider: relay\nprovider_options:\n  temperature: ${temperature}\n`;
const memoryNamespace = (key: string, label: string): api.MemoryNamespace => ({ key, namespace: key, kind: 'conversation', subject: '', entry_count: 1, books: [], updated_at: '', owner: { kind: 'group', label } });
const memoryEntry = (content: string): api.MemoryEntry => ({ id: `id-${content}`, book: 'main', content, keywords: [], constant: false, source: 'manual' });
const scopeStatus = (target = ''): api.ScopeStatus => ({
  current_scope: '10001', live_scope: '', bot_alive: false,
  preview: target ? { source_scope: '10001', target_scope: target, conversations: [{ old_namespace: 'old-namespace', new_namespace: `${target}-namespace`, has_memory: true, blocked: false }], unknown_namespaces: [] } : null,
});
const conversation = (id: string): api.ConversationRow => ({ session_id: id, conversation_key: `qq_group:${id}`, channel: 'qq', bytes: 128, updated_at: 0, owner: { kind: 'group', label: `会话${id}` }, current_account: true });

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'fixture-auth', isAuthenticated: true });
  vi.mocked(api.getProviderProfiles).mockResolvedValue(structuredClone(profiles));
  vi.mocked(api.getProviders).mockResolvedValue(providers());
  vi.mocked(api.getNodes).mockResolvedValue([{ id: 'node.a', provider: 'relay' }, { id: 'node.b', provider: 'relay' }] as api.AdminNode[]);
  vi.mocked(api.getRuntimeRaw).mockResolvedValue(runtime);
  vi.mocked(api.getNodeRaw).mockImplementation(async (_auth, id) => nodeRaw(id, id === 'node.a' ? 0.4 : 0.7));
  vi.mocked(api.updateRuntimeRaw).mockResolvedValue({}); vi.mocked(api.updateNodeRaw).mockResolvedValue({});
  vi.mocked(api.upsertProvider).mockResolvedValue(providers());
  vi.mocked(api.setActiveProvider).mockResolvedValue({ ...providers(), active_provider: 'spare' });
  vi.mocked(api.deleteProvider).mockResolvedValue(providers());
  vi.mocked(api.getMemoryOverview).mockResolvedValue([memoryNamespace('ns-a', '来源甲'), memoryNamespace('ns-b', '来源乙')]);
  vi.mocked(api.getMemoryEntries).mockResolvedValue([]);
  vi.mocked(api.saveMemoryEntry).mockResolvedValue(memoryEntry('已保存'));
  vi.mocked(api.getConversations).mockResolvedValue([]);
  vi.mocked(api.getConversationMessages).mockResolvedValue({ total: 0, messages: [] });
  vi.mocked(api.getQqScope).mockImplementation(async (_auth, target) => scopeStatus(target));
  vi.mocked(api.migrateQqScope).mockResolvedValue({ moved_memory_dirs: 1, sessions_changed: 1, backup_dir: 'synthetic-backup' });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

async function openModels() {
  render(<ModelsPage />);
  await screen.findByRole('button', { name: 'node.a' });
  await screen.findByLabelText('采样温度');
}
const pickNode = (name: string) => fireEvent.click(within(screen.getByRole('group', { name: '参数作用域' })).getByRole('button', { name }));
const pickFormat = (name: string) => fireEvent.click(within(screen.getByRole('group', { name: '参数格式' })).getByRole('button', { name }));

describe('模型参数请求与草稿归属', () => {
  it.each(['resolve', 'reject'] as const)('拒绝旧节点的迟到 %s，不覆盖当前节点参数', async outcome => {
    const old = deferred<string>();
    vi.mocked(api.getNodeRaw).mockImplementation(async (_auth, id) => id === 'node.a' ? old.promise : nodeRaw(id, 0.7));
    await openModels(); pickNode('node.a');
    expect(screen.queryByLabelText('采样温度')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存参数' })).toBeDisabled();
    pickNode('node.b'); await waitFor(() => expect(screen.getByLabelText('采样温度')).toHaveValue('0.7'));
    await act(async () => outcome === 'resolve' ? old.resolve(nodeRaw('node.a', 0.4)) : old.reject(Error('旧节点读取失败')));
    expect(screen.getByLabelText('采样温度')).toHaveValue('0.7');
    expect(screen.queryByText('旧节点读取失败')).not.toBeInTheDocument();
  });
  it('通过渠道别名选择正确线格式，拒绝丢稿时不切节点或格式', async () => {
    await openModels(); expect(screen.getByLabelText('采样温度')).toHaveValue('0.2');
    fireEvent.change(screen.getByLabelText('采样温度'), { target: { value: '0.6' } });
    vi.mocked(window.confirm).mockReturnValue(false); pickNode('node.a'); pickFormat('anthropic');
    expect(screen.getByLabelText('采样温度')).toHaveValue('0.6'); expect(api.getNodeRaw).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('另一格式温度')).not.toBeInTheDocument();
    vi.mocked(window.confirm).mockReturnValue(true); pickFormat('anthropic');
    expect(screen.getByLabelText('另一格式温度')).toHaveValue('0.9');
  });
  it('保存期间冻结参数及切换入口，失败保留草稿供重试', async () => {
    const write = deferred<unknown>(); vi.mocked(api.updateRuntimeRaw).mockReturnValue(write.promise);
    await openModels(); fireEvent.change(screen.getByLabelText('采样温度'), { target: { value: '0.6' } });
    fireEvent.click(screen.getByRole('button', { name: '保存参数' }));
    expect(screen.getByLabelText('采样温度')).toBeDisabled(); expect(screen.getByRole('button', { name: 'node.a' })).toBeDisabled();
    await act(async () => write.reject(Error('写入被拒绝')));
    expect(await screen.findByText('写入被拒绝')).toBeInTheDocument(); expect(screen.getByLabelText('采样温度')).toHaveValue('0.6');
    expect(screen.getByRole('button', { name: '保存参数' })).toBeEnabled();
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('fixture-auth', expect.stringContaining('temperature: 0.6'));
    expect(api.updateNodeRaw).not.toHaveBeenCalled();
  });
});

describe('渠道响应与未保存配置', () => {
  it('切换活跃渠道的响应不会覆盖其他渠道的本地草稿', async () => {
    render(<ProvidersPage />); fireEvent.change(await screen.findByLabelText('relay 模型'), { target: { value: 'unsaved-model' } });
    const row = screen.getByLabelText('spare 模型').closest('li')!;
    fireEvent.click(within(row).getByRole('button', { name: '设为活跃' }));
    await screen.findByText('已切到 spare。'); expect(screen.getByLabelText('relay 模型')).toHaveValue('unsaved-model');
    expect(screen.getByRole('button', { name: '保存渠道' })).toBeEnabled();
  });
  it('提交期间冻结新增入口，失败后保留模型和密钥草稿', async () => {
    const write = deferred<api.ProvidersResponse>(); vi.mocked(api.upsertProvider).mockReturnValue(write.promise);
    render(<ProvidersPage />); fireEvent.change(await screen.findByLabelText('relay 模型'), { target: { value: 'unsaved-model' } });
    fireEvent.change(screen.getByLabelText('relay 密钥'), { target: { value: 'synthetic-key' } });
    fireEvent.change(screen.getByLabelText('新渠道名'), { target: { value: 'third' } });
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'openai' } });
    fireEvent.click(screen.getByRole('button', { name: '保存渠道' }));
    expect(screen.getByLabelText('relay 模型')).toBeDisabled();
    expect(screen.getByLabelText('新渠道名')).toBeDisabled(); expect(screen.getByLabelText('新渠道格式')).toBeDisabled();
    expect(screen.getByRole('button', { name: '添加' })).toBeDisabled();
    await act(async () => write.reject(Error('渠道写入失败')));
    await screen.findByText('渠道写入失败'); expect(screen.getByLabelText('relay 模型')).toHaveValue('unsaved-model');
    expect(screen.getByLabelText('relay 密钥')).toHaveValue('synthetic-key');
  });
  it.each(['resolve', 'reject'] as const)('更换身份后忽略旧渠道列表的迟到 %s', async outcome => {
    const old = deferred<api.ProvidersResponse>();
    vi.mocked(api.getProviders).mockImplementation(async token => token === 'fixture-auth' ? old.promise : { ...providers(), providers: { current: providerBlock('current') } });
    render(<ProvidersPage />); await act(async () => useSettingsStore.setState({ adminToken: 'fixture-new' }));
    await screen.findByLabelText('current 模型'); fireEvent.change(screen.getByLabelText('current 模型'), { target: { value: 'current-draft' } });
    await act(async () => outcome === 'resolve' ? old.resolve(providers()) : old.reject(Error('旧身份不可用')));
    expect(screen.getByLabelText('current 模型')).toHaveValue('current-draft');
    expect(screen.queryByLabelText('relay 模型')).not.toBeInTheDocument(); expect(screen.queryByText('旧身份不可用')).not.toBeInTheDocument();
  });
  it('拒绝还原时保留渠道草稿', async () => {
    render(<ProvidersPage />); fireEvent.change(await screen.findByLabelText('relay 模型'), { target: { value: 'keep-me' } });
    vi.mocked(window.confirm).mockReturnValue(false); fireEvent.click(screen.getByRole('button', { name: '还原' }));
    expect(screen.getByLabelText('relay 模型')).toHaveValue('keep-me'); expect(api.upsertProvider).not.toHaveBeenCalled();
  });
});

describe('记忆来源与上下文读取', () => {
  it('同名会话的删除确认与列表一致，请求仍指向选中的session', async () => {
    const a = { ...conversation('a'), owner: { kind: 'group' as const, label: '同名群' } };
    const b = { ...conversation('b'), owner: { kind: 'group' as const, label: '同名群' }, current_account: false };
    vi.mocked(api.getConversations).mockResolvedValue([a, b]);
    vi.mocked(api.resetConversationBySession).mockResolvedValue({});
    render(<MemoryPage />); const titles = await screen.findAllByText(/^同名群/);
    const label = titles.find(item => item.textContent?.includes('历史账号'))!;
    fireEvent.click(within(label.parentElement!).getByRole('button', { name: '删除' }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining(`「${label.textContent}」`));
    await waitFor(() => expect(api.resetConversationBySession).toHaveBeenCalledWith('fixture-auth', 'b'));
  });
  it('更换认证身份立即清空旧名称和预览，旧列表响应不能回流', async () => {
    const old = deferred<api.ConversationRow[]>();
    vi.mocked(api.getConversations).mockReturnValueOnce(old.promise).mockResolvedValueOnce([conversation('新账号')]);
    render(<MemoryPage />); await screen.findByText('来源甲');
    vi.mocked(api.getMemoryOverview).mockResolvedValueOnce([memoryNamespace('new', '新身份来源')]);
    act(() => useSettingsStore.setState({ adminToken: 'new-auth' }));
    expect(screen.queryByText('来源甲')).not.toBeInTheDocument();
    await screen.findByText('会话新账号');
    await act(async () => old.resolve([conversation('旧账号')]));
    expect(screen.queryByText('会话旧账号')).not.toBeInTheDocument(); expect(screen.getByText('新身份来源')).toBeInTheDocument();
  });
  it.each(['resolve', 'reject'] as const)('来源切换拒绝旧条目的迟到 %s', async outcome => {
    const old = deferred<api.MemoryEntry[]>();
    vi.mocked(api.getMemoryEntries).mockImplementation(async (_auth, source) => source === 'ns-a' ? old.promise : [memoryEntry('乙来源内容')]);
    render(<MemoryPage />); fireEvent.click(await screen.findByRole('button', { name: /来源甲/ }));
    expect(screen.getByLabelText('内容')).toBeDisabled(); fireEvent.click(screen.getByRole('button', { name: /来源乙/ }));
    await screen.findByText('乙来源内容');
    await act(async () => outcome === 'resolve' ? old.resolve([memoryEntry('甲来源内容')]) : old.reject(Error('甲来源失败')));
    expect(screen.getByText('乙来源内容')).toBeInTheDocument(); expect(screen.queryByText('甲来源内容')).not.toBeInTheDocument();
    expect(screen.queryByText('甲来源失败')).not.toBeInTheDocument(); expect(screen.getByLabelText('内容')).toBeEnabled();
  });
  it('拒绝丢弃时保持原记忆来源和草稿', async () => {
    render(<MemoryPage />); fireEvent.click(await screen.findByRole('button', { name: /来源甲/ }));
    await waitFor(() => expect(screen.getByLabelText('内容')).toBeEnabled()); fireEvent.change(screen.getByLabelText('内容'), { target: { value: '未保存记忆' } });
    vi.mocked(window.confirm).mockReturnValue(false); fireEvent.click(screen.getByRole('button', { name: /来源乙/ }));
    expect(screen.getByLabelText('内容')).toHaveValue('未保存记忆');
    expect(api.getMemoryEntries).not.toHaveBeenCalledWith('fixture-auth', 'ns-b');
  });
  it('添加记忆时锁定来源和表单，失败后内容完整保留', async () => {
    const write = deferred<api.MemoryEntry>(); vi.mocked(api.saveMemoryEntry).mockReturnValue(write.promise);
    render(<MemoryPage />); fireEvent.click(await screen.findByRole('button', { name: /来源甲/ }));
    await waitFor(() => expect(screen.getByLabelText('内容')).toBeEnabled());
    fireEvent.change(screen.getByLabelText('标识'), { target: { value: 'fixture_entry' } });
    fireEvent.change(screen.getByLabelText('内容'), { target: { value: '未保存记忆' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getByLabelText('内容')).toBeDisabled(); expect(screen.getByRole('button', { name: /来源乙/ })).toBeDisabled();
    await act(async () => write.reject(Error('记忆写入失败'))); await screen.findByText('记忆写入失败');
    expect(screen.getByLabelText('内容')).toHaveValue('未保存记忆'); expect(screen.getByLabelText('标识')).toHaveValue('fixture_entry');
  });
  it('上下文预览保持最后选择的会话，不被旧会话回包覆盖', async () => {
    const old = deferred<{ total: number; messages: Array<Record<string, unknown>> }>();
    vi.mocked(api.getConversations).mockResolvedValue([conversation('甲'), conversation('乙')]);
    vi.mocked(api.getConversationMessages).mockImplementation(async (_auth, id) => id === '甲' ? old.promise : { total: 1, messages: [{ role: 'user', content: '乙会话原文' }] });
    render(<MemoryPage />); await screen.findByText('会话甲');
    const contextRow = (label: string) => screen.getByText(label).parentElement!.parentElement!;
    fireEvent.click(within(contextRow('会话甲')).getByRole('button', { name: '查看' }));
    fireEvent.click(within(contextRow('会话乙')).getByRole('button', { name: '查看' }));
    await screen.findByText(/乙会话原文/); await act(async () => old.resolve({ total: 1, messages: [{ role: 'user', content: '甲会话原文' }] }));
    expect(screen.getByText(/乙会话原文/)).toBeInTheDocument(); expect(screen.queryByText(/甲会话原文/)).not.toBeInTheDocument();
  });
});

describe('迁移预览目标绑定', () => {
  it('目标变更立即作废旧预览，必须重新预览才能迁移', async () => {
    render(<MemoryPage />); await screen.findByText('10001');
    fireEvent.change(screen.getByLabelText('搬到'), { target: { value: '20002' } }); fireEvent.click(screen.getByRole('button', { name: '预览' }));
    await screen.findByRole('button', { name: '执行搬迁' });
    fireEvent.change(screen.getByLabelText('搬到'), { target: { value: '30003' } });
    expect(screen.queryByRole('button', { name: '执行搬迁' })).not.toBeInTheDocument(); expect(api.migrateQqScope).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '预览' })); fireEvent.click(await screen.findByRole('button', { name: '执行搬迁' }));
    await waitFor(() => expect(api.migrateQqScope).toHaveBeenCalledWith('fixture-auth', '30003'));
    expect(api.migrateQqScope).toHaveBeenCalledTimes(1);
  });
  it('预览和迁移期间禁用目标输入，失败保留受绑定的目标供重试', async () => {
    const preview = deferred<api.ScopeStatus>(); const write = deferred<Awaited<ReturnType<typeof api.migrateQqScope>>>();
    vi.mocked(api.getQqScope).mockImplementation(async (_auth, target) => target ? preview.promise : scopeStatus());
    vi.mocked(api.migrateQqScope).mockReturnValue(write.promise);
    render(<MemoryPage />); await screen.findByText('10001');
    fireEvent.change(screen.getByLabelText('搬到'), { target: { value: ' 20002 ' } }); fireEvent.click(screen.getByRole('button', { name: '预览' }));
    expect(screen.getByLabelText('搬到')).toBeDisabled();
    await act(async () => preview.resolve(scopeStatus('20002')));
    fireEvent.click(screen.getByRole('button', { name: '执行搬迁' })); expect(screen.getByLabelText('搬到')).toBeDisabled();
    expect(api.migrateQqScope).toHaveBeenCalledWith('fixture-auth', '20002');
    await act(async () => write.reject(Error('迁移失败'))); await screen.findByText('迁移失败');
    expect(screen.getByLabelText('搬到')).toHaveValue(' 20002 '); expect(screen.getByLabelText('搬到')).toBeEnabled();
    expect(screen.getByRole('button', { name: '执行搬迁' })).toBeEnabled();
  });
});
