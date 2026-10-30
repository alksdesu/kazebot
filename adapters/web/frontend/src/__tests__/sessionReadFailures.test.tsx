// 顶栏和会话面板读不到东西时：说没读到、给重试，不拿入口节点、节点模型这些下一层的值冒充，
// 也不把猜的值写进 store；两处各读各的，谁先读成都一样。
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RequestScope } from '../api/requestGuard';
import * as api from '../api/supervisorClient';
import type { AdminNode, ProvidersResponse } from '../api/supervisorClient';
import { Header } from '../components/layout';
import { SessionConfigPanel } from '../components/settings/SessionConfigPanel';
import { useChatStore } from '../store/chatStore';
import { readActiveNode, readGlobalConfig, readNodeList, readSessionOverride } from '../store/settingsReads';
import { LOADED, UNREAD, useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getActiveNode: vi.fn(),
  getAppConfig: vi.fn(),
  getNodes: vi.fn(),
  getProviders: vi.fn(),
  getSessionProviderOverride: vi.fn(),
  switchNode: vi.fn(),
}));

const block = (model: string, base_url: string) => ({
  model, base_url, model_raw: model, base_url_raw: base_url,
  api_key_present: true, api_key_redacted: '****', supports_vision: null, type: '', type_explicit: false, label: '',
});

const NODES: AdminNode[] = [
  { id: 'main', type: 'ai', name: 'Main', model: 'main-model', base_url: 'https://main.example/v1' },
  { id: 'entry', type: 'ai', name: 'Entry', model: 'entry-model' },
  { id: 'relayed', type: 'ai', name: 'Relayed', provider: 'relay' },
  { id: 'worker', type: 'ai', name: 'Worker' },
  { id: 'boss', type: 'ai', name: 'Boss', delegate_targets: ['worker'] },
];

const PROVIDERS = {
  active_provider: 'openai',
  providers: {
    openai: block('global-model', 'https://global.example/v1'),
    relay: block('relay-model', 'https://relay.example/v1'),
  },
  fallbacks: [],
  node_fallbacks: {},
  registered: ['openai', 'anthropic'],
  reserved_names: [],
} as unknown as ProvidersResponse;

const GLOBAL_CONFIG = { provider: 'openai', openai: { model: 'global-model', base_url: 'https://global.example/v1' }, default_entry_node_id: 'main' };

// 改过名的节点：会话还记着 aliases 里的旧 id
const RENAMED: AdminNode = { id: 'renamed', type: 'ai', name: 'Renamed', aliases: ['old.renamed'], model: 'renamed-model' };

type ActiveNode = Awaited<ReturnType<typeof api.getActiveNode>>;

const activeNode = (nodeId: string, isOverride = false): ActiveNode => ({ node_id: nodeId, is_override: isOverride, default_node_id: 'main' });

const renderHeader = () => render(<Header isGenerating={false} sessionId="session-1" title="对话" />);
const nodeLabel = () => screen.getByTitle('切换节点');
const modelLabel = () => screen.getByTitle('模型配置');
const nodeTitle = () => screen.getByText('活动节点').nextElementSibling as HTMLElement;

beforeEach(() => {
  localStorage.clear();
  useSettingsStore.getState().clearLoginData();
  useSettingsStore.setState({ adminToken: 'test-token', isAuthenticated: true, entryNodeId: 'entry', rightPanelOpen: true });
  useChatStore.setState({ connectionStatus: 'open' });
  vi.mocked(api.getActiveNode).mockReset().mockResolvedValue(activeNode('main'));
  vi.mocked(api.getSessionProviderOverride).mockReset().mockResolvedValue({});
  vi.mocked(api.getAppConfig).mockReset().mockResolvedValue(GLOBAL_CONFIG);
  vi.mocked(api.getNodes).mockReset().mockResolvedValue(structuredClone(NODES));
  vi.mocked(api.getProviders).mockReset().mockResolvedValue(structuredClone(PROVIDERS));
  vi.mocked(api.switchNode).mockReset();
});

afterEach(() => {
  useChatStore.getState().resetState();
});

describe('顶栏', () => {
  it('当前节点没读到时说没读到，不拿入口节点顶上，重试读到了就恢复', async () => {
    vi.mocked(api.getActiveNode).mockRejectedValueOnce(new Error('active_node 502'));
    renderHeader();

    const retry = await screen.findByRole('button', { name: '当前节点读取失败，重试' });
    expect(retry).toHaveAttribute('title', '当前节点：active_node 502');
    expect(nodeLabel()).toHaveTextContent('节点没读到');
    expect(nodeLabel()).not.toHaveTextContent('Entry');
    await waitFor(() => expect(modelLabel()).toHaveTextContent('模型没读到'));
    expect(modelLabel()).not.toHaveTextContent('entry-model');

    fireEvent.click(retry);

    await waitFor(() => expect(nodeLabel()).toHaveTextContent('Main'));
    expect(modelLabel()).toHaveTextContent('main-model');
    expect(screen.queryByRole('button', { name: /读取失败/ })).toBeNull();
    expect(useSettingsStore.getState()).toMatchObject({ activeNodeId: 'main', activeNodeSessionId: 'session-1' });
  });

  it('会话覆盖没读到时模型说没读到，不拿节点的模型冒充，也不把「没有覆盖」写进 store', async () => {
    vi.mocked(api.getSessionProviderOverride).mockRejectedValue(new Error('override 500'));
    renderHeader();

    await screen.findByRole('button', { name: '会话覆盖读取失败，重试' });
    await waitFor(() => expect(nodeLabel()).toHaveTextContent('Main'));
    expect(modelLabel()).toHaveTextContent('模型没读到');
    expect(modelLabel()).not.toHaveTextContent('main-model');
    expect(useSettingsStore.getState().sessionProviderOverrideSessionId).toBe('');
  });

  it('节点清单没读到时节点照样显示 id，模型说没读到，重试读到了补上', async () => {
    vi.mocked(api.getNodes).mockRejectedValueOnce(new Error('nodes 503'));
    renderHeader();

    const retry = await screen.findByRole('button', { name: '节点清单读取失败，重试' });
    expect(nodeLabel()).toHaveTextContent('main');
    expect(modelLabel()).toHaveTextContent('模型没读到');

    fireEvent.click(retry);

    await waitFor(() => expect(modelLabel()).toHaveTextContent('main-model'));
    expect(nodeLabel()).toHaveTextContent('Main');
    expect(screen.queryByRole('button', { name: /读取失败/ })).toBeNull();
  });

  it('节点只点名了渠道时用那个渠道块的模型', async () => {
    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('relayed', true));
    renderHeader();

    await waitFor(() => expect(modelLabel()).toHaveTextContent('relay-model'));
  });

  it('要用渠道块而渠道表没读到时说渠道表', async () => {
    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('relayed', true));
    vi.mocked(api.getProviders).mockRejectedValue(new Error('providers 403'));
    renderHeader();

    await screen.findByRole('button', { name: '渠道表读取失败，重试' });
    expect(modelLabel()).toHaveTextContent('模型没读到');
    expect(modelLabel()).not.toHaveTextContent('global-model');
  });

  it('全局配置没读到只在模型真追到全局那一层时才说', async () => {
    vi.mocked(api.getAppConfig).mockRejectedValue(new Error('config 500'));
    const view = renderHeader();

    await waitFor(() => expect(modelLabel()).toHaveTextContent('main-model'));
    expect(screen.queryByRole('button', { name: /读取失败/ })).toBeNull();
    view.unmount();

    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('worker', true));
    renderHeader();

    await screen.findByRole('button', { name: '全局配置读取失败，重试' });
    expect(modelLabel()).toHaveTextContent('模型没读到');
  });

  it('会话记着节点的旧 id 时照常显示那个节点和它的模型', async () => {
    vi.mocked(api.getNodes).mockResolvedValue([...structuredClone(NODES), RENAMED]);
    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('old.renamed', true));
    renderHeader();

    await waitFor(() => expect(modelLabel()).toHaveTextContent('renamed-model'));
    expect(nodeLabel()).toHaveTextContent('Renamed');
  });

  it('节点表里认不出会话的节点时说找不到，模型不拿全局的冒充，也不当读取失败', async () => {
    // yaml 里写的 id 不算：engine 按它找不到节点
    vi.mocked(api.getNodes).mockResolvedValue([...structuredClone(NODES), { id: 'decorated', type: 'ai', declared_id: 'gone.node', model: 'decorated-model' }]);
    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('gone.node', true));
    renderHeader();

    await waitFor(() => expect(nodeLabel()).toHaveTextContent('gone.node（找不到这个节点）'));
    await waitFor(() => expect(useSettingsStore.getState().globalConfigRead).toBe(LOADED));
    expect(modelLabel()).toHaveTextContent('节点找不到');
    expect(modelLabel()).not.toHaveTextContent('global-model');
    expect(modelLabel()).not.toHaveTextContent('decorated-model');
    expect(screen.queryByRole('button', { name: /读取失败/ })).toBeNull();
  });

  it('顶栏的节点读取晚到时不盖掉面板刚切好的节点', async () => {
    let finishHeader!: (value: ActiveNode) => void;
    vi.mocked(api.getActiveNode)
      .mockReturnValueOnce(new Promise((resolve) => { finishHeader = resolve; }))
      .mockResolvedValue(activeNode('main'));
    vi.mocked(api.switchNode).mockResolvedValue({ ok: true, target_node_id: 'entry', is_override: true, default_node_id: 'main' });
    renderHeader();
    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    const select = container.querySelector('select')!;
    await waitFor(() => expect(select).toBeEnabled());
    await waitFor(() => expect(within(select).getByRole('option', { name: 'Entry' })).toBeInTheDocument());
    fireEvent.change(select, { target: { value: 'entry' } });
    await waitFor(() => expect(useSettingsStore.getState().activeNodeId).toBe('entry'));

    await act(async () => finishHeader(activeNode('main')));

    expect(useSettingsStore.getState()).toMatchObject({ activeNodeId: 'entry', activeNodeIsOverride: true, activeNodeSessionId: 'session-1' });
    expect(nodeLabel()).toHaveTextContent('Entry');
    expect(nodeTitle()).toHaveTextContent('Entry');
    expect(screen.queryByRole('button', { name: /读取失败/ })).toBeNull();
  });

  it('顶栏没读成、会话面板读成了：顶栏的失败提示自己消失', async () => {
    vi.mocked(api.getActiveNode).mockRejectedValueOnce(new Error('active_node 502'));
    renderHeader();
    await screen.findByRole('button', { name: '当前节点读取失败，重试' });

    fireEvent.click(nodeLabel());

    // 弹层打开后顶栏被标成 aria-hidden，按角色找不到它，只能按文字找
    await waitFor(() => expect(nodeLabel()).toHaveTextContent('Main'));
    expect(screen.queryByText('当前节点读取失败，重试')).toBeNull();
  });
});

describe('会话面板', () => {
  it('当前节点没读到时下拉禁用并说没读到，重试读到了就能切', async () => {
    vi.mocked(api.getActiveNode).mockRejectedValueOnce(new Error('active_node 502'));
    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('当前节点读取失败：active_node 502');
    const select = container.querySelector('select')!;
    expect(select).toBeDisabled();
    expect(within(select).getByRole('option', { name: '当前节点没读到' })).toBeInTheDocument();
    expect(nodeTitle()).toHaveTextContent('当前节点没读到');

    fireEvent.click(within(alert).getByRole('button', { name: '重试' }));

    await waitFor(() => expect(select).toBeEnabled());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(nodeTitle()).toHaveTextContent('Main');
  });

  it('会话的节点还没读到时不拿入口节点的模型顶上', async () => {
    let finish!: (value: ActiveNode) => void;
    vi.mocked(api.getActiveNode).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(useSettingsStore.getState().nodeListRead).toBe(LOADED));
    expect(screen.queryByText('entry-model')).toBeNull();
    expect(screen.getByLabelText('模型')).toHaveAttribute('placeholder', '继承的模型（读取中）');
    expect(nodeTitle()).toHaveTextContent('读取节点…');
    expect(container.querySelector('select')).toBeDisabled();

    await act(async () => finish(activeNode('main')));

    expect(await screen.findByText('main-model')).toBeInTheDocument();
  });

  it('会话覆盖没读到时输入框不让填，生效值不拿节点的顶上，重试读到了就能填', async () => {
    vi.mocked(api.getSessionProviderOverride).mockRejectedValueOnce(new Error('override 500'));
    render(<SessionConfigPanel sessionId="session-1" />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('会话覆盖读取失败：override 500');
    expect(screen.getByLabelText('模型')).toBeDisabled();
    expect(screen.queryByText('main-model')).toBeNull();
    expect(screen.getAllByText('（会话覆盖读取失败）')).toHaveLength(2);
    expect(useSettingsStore.getState().sessionProviderOverrideSessionId).toBe('');

    vi.mocked(api.getSessionProviderOverride).mockResolvedValue({ model: 'session-model' });
    fireEvent.click(within(alert).getByRole('button', { name: '重试' }));

    await waitFor(() => expect(screen.getByLabelText('模型')).toBeEnabled());
    expect(screen.getByLabelText('模型')).toHaveValue('session-model');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('顶栏读成了覆盖而面板自己没读成：生效值用读到的那份，编辑要等面板自己读成', async () => {
    useSettingsStore.getState().setSessionProviderOverride({ model: 'session-model' }, 'session-1');
    vi.mocked(api.getSessionProviderOverride).mockRejectedValueOnce(new Error('override 500'));
    render(<SessionConfigPanel sessionId="session-1" />);

    expect(await screen.findByRole('alert')).toHaveTextContent('会话覆盖读取失败');
    expect(screen.getByText('session-model')).toBeInTheDocument();
    expect(screen.getByLabelText('模型')).toBeDisabled();
  });

  it('节点清单没读到时说出来，生效值停在节点那一层，重试读到了补上', async () => {
    vi.mocked(api.getNodes).mockRejectedValueOnce(new Error('nodes 503'));
    render(<SessionConfigPanel sessionId="session-1" />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('节点清单读取失败：nodes 503');
    await waitFor(() => expect(screen.getAllByText('（节点读取失败）')).toHaveLength(2));

    fireEvent.click(within(alert).getByRole('button', { name: '重试' }));

    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    expect(await screen.findByText('main-model')).toBeInTheDocument();
  });

  it('节点清单还在读时下拉里说正在读', () => {
    vi.mocked(api.getNodes).mockReturnValue(new Promise(() => {}));
    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    const select = container.querySelector('select')!;
    expect(within(select).getByRole('option', { name: '正在读取节点清单…' })).toBeDisabled();
  });

  it('会话被切到不在可切换清单里的节点上时，下拉照实显示它', async () => {
    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('worker', true));
    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    const select = container.querySelector('select')!;
    await waitFor(() => expect(within(select).getByRole('option', { name: 'Worker（不在可切换的节点里）' })).toBeInTheDocument());
    expect(select).toHaveValue('worker');
    expect(within(select).queryByRole('option', { name: 'Worker' })).toBeNull();
  });

  it('节点只点名了渠道时生效值标出是哪个渠道块', async () => {
    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('relayed', true));
    render(<SessionConfigPanel sessionId="session-1" />);

    expect(await screen.findByText('relay-model')).toBeInTheDocument();
    expect(screen.getByText('https://relay.example/v1')).toBeInTheDocument();
    expect(screen.getAllByText('（渠道：relay）')).toHaveLength(2);
  });

  it('会话自己点名渠道时，模型框留空继承的是那个渠道块的模型', async () => {
    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('worker', true));
    render(<SessionConfigPanel sessionId="session-1" />);

    const model = screen.getByLabelText('模型');
    await waitFor(() => expect(model).toBeEnabled());
    await waitFor(() => expect(model).toHaveAttribute('placeholder', 'global-model'));

    fireEvent.change(screen.getByLabelText('供应商'), { target: { value: 'relay' } });

    expect(model).toHaveAttribute('placeholder', 'relay-model');
  });

  it('会话记着节点的旧 id 时生效值取那一行，下拉对得上那一项', async () => {
    vi.mocked(api.getNodes).mockResolvedValue([...structuredClone(NODES), RENAMED]);
    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('old.renamed', true));
    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    expect(await screen.findByText('renamed-model')).toBeInTheDocument();
    expect(screen.getByText('（节点：renamed）')).toBeInTheDocument();
    const select = container.querySelector('select')!;
    expect(select).toHaveValue('renamed');
    expect(within(select).queryByRole('option', { name: /不在可切换/ })).toBeNull();
    expect(nodeTitle()).toHaveTextContent('Renamed');
  });

  it('节点表里没有会话的节点时生效值说节点找不到、标出是哪个节点，不报读取失败', async () => {
    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('gone.node', true));
    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(screen.getAllByText('（节点 gone.node 不在节点表里）')).toHaveLength(2));
    expect(screen.getAllByText('节点找不到')).toHaveLength(2);
    expect(screen.getByLabelText('模型')).toHaveAttribute('placeholder', '继承的模型（节点找不到）');
    expect(screen.getByLabelText('供应商')).toHaveAttribute('placeholder', '继承的供应商（节点找不到）');
    await waitFor(() => expect(useSettingsStore.getState().globalConfigRead).toBe(LOADED));
    expect(screen.queryByText('global-model')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(nodeTitle()).toHaveTextContent('gone.node（找不到这个节点）');
    const select = container.querySelector('select')!;
    expect(select).toHaveValue('gone.node');
    expect(within(select).getByRole('option', { name: 'gone.node（找不到这个节点）' })).toBeInTheDocument();
  });

  it('面板自己的节点读取晚到时也盖不回刚切好的节点', async () => {
    useSettingsStore.getState().setActiveNode('main', false, 'main', 'session-1');
    let finish!: (value: ActiveNode) => void;
    vi.mocked(api.getActiveNode).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    vi.mocked(api.switchNode).mockResolvedValue({ ok: true, target_node_id: 'entry', is_override: true, default_node_id: 'main' });
    const { container } = render(<SessionConfigPanel sessionId="session-1" />);

    const select = container.querySelector('select')!;
    await waitFor(() => expect(within(select).getByRole('option', { name: 'Entry' })).toBeInTheDocument());
    fireEvent.change(select, { target: { value: 'entry' } });
    await waitFor(() => expect(useSettingsStore.getState().activeNodeId).toBe('entry'));

    await act(async () => finish(activeNode('main')));

    expect(useSettingsStore.getState()).toMatchObject({ activeNodeId: 'entry', activeNodeIsOverride: true });
  });

  // 切换已经在服务端生效：当成失败的话，人会再切一次，下拉框还停在切之前。
  it('切好了却读不懂回执：照实说一声，并重读当前节点', async () => {
    vi.mocked(api.switchNode).mockRejectedValue(new api.ReceiptUnreadable('节点已经切换，可回执格式不对：缺 target_node_id'));
    const { container } = render(<SessionConfigPanel sessionId="session-1" />);
    await waitFor(() => expect(api.getActiveNode).toHaveBeenCalledTimes(1));
    vi.mocked(api.getActiveNode).mockResolvedValue(activeNode('entry', true));

    const select = container.querySelector('select')!;
    await waitFor(() => expect(within(select).getByRole('option', { name: 'Entry' })).toBeInTheDocument());
    fireEvent.change(select, { target: { value: 'entry' } });

    expect(await screen.findByText('节点已经切换，可回执格式不对：缺 target_node_id。下拉框里是重新读到的当前节点')).toBeInTheDocument();
    await waitFor(() => expect(api.getActiveNode).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(useSettingsStore.getState()).toMatchObject({ activeNodeId: 'entry', activeNodeIsOverride: true }));
  });

  it('会话覆盖存上了却读不懂回执：锁住编辑重读一遍，不当成保存失败', async () => {
    vi.mocked(api.getSessionProviderOverride).mockResolvedValueOnce({}).mockResolvedValue({ model: 'saved-model' });
    const update = vi.spyOn(api, 'updateSessionProviderOverride')
      .mockRejectedValue(new api.ReceiptUnreadable('会话的渠道覆盖已经存上，可回执格式不对：返回的不是 JSON 对象'));
    try {
      render(<SessionConfigPanel sessionId="session-1" focus="model" />);

      const model = await screen.findByLabelText('模型');
      await waitFor(() => expect(model).toBeEnabled());
      fireEvent.change(model, { target: { value: 'saved-model' } });
      fireEvent.click(screen.getByRole('button', { name: '保存' }));

      expect(await screen.findByText('会话的渠道覆盖已经存上，可回执格式不对：返回的不是 JSON 对象。会话覆盖以重新读到的为准')).toBeInTheDocument();
      expect(update).toHaveBeenCalledOnce();
      await waitFor(() => expect(api.getSessionProviderOverride).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(screen.getByLabelText('模型')).toHaveValue('saved-model'));
    } finally {
      update.mockRestore();
    }
  });
});

describe('共用读取', () => {
  // 形状由接口函数核，这里只管核不过的那句原样记下、不往 store 里写猜的值。
  it('回来的格式不对也算没读成，不当成空的写进 store', async () => {
    vi.mocked(api.getAppConfig).mockRejectedValue(new Error('全局配置格式不对：缺 default_entry_node_id'));
    vi.mocked(api.getActiveNode).mockRejectedValue(new Error('会话的当前节点格式不对：缺 node_id'));
    vi.mocked(api.getSessionProviderOverride).mockRejectedValue(new Error('会话的渠道覆盖格式不对：返回的不是 JSON 对象'));
    const scope = new RequestScope();

    await readGlobalConfig(scope.begin('config'));
    const node = await readActiveNode('session-1', scope.begin('node'));
    const override = await readSessionOverride('session-1', 'test-token', scope.begin('override'));

    const state = useSettingsStore.getState();
    expect(state.globalConfigRead).toEqual({ loaded: false, error: '全局配置格式不对：缺 default_entry_node_id' });
    expect(node).toEqual({ ok: false, error: '会话的当前节点格式不对：缺 node_id' });
    expect(override).toEqual({ ok: false, error: '会话的渠道覆盖格式不对：返回的不是 JSON 对象' });
    expect(state.activeNodeSessionId).toBe('');
    expect(state.sessionProviderOverrideSessionId).toBe('');
  });

  it('读当前节点的期间有人写过，读到的旧值照常交给调用方，不写进 store', async () => {
    let finish!: (value: ActiveNode) => void;
    vi.mocked(api.getActiveNode).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const reading = readActiveNode('session-1', new RequestScope().begin('node'));

    useSettingsStore.getState().setActiveNode('entry', true, 'main', 'session-1');
    finish(activeNode('main'));

    expect(await reading).toEqual({ ok: true, value: 'main' });
    expect(useSettingsStore.getState()).toMatchObject({ activeNodeId: 'entry', activeNodeIsOverride: true });
  });

  it('全局配置读成了把后端配的默认入口记下；读不成时不记', async () => {
    const scope = new RequestScope();
    vi.mocked(api.getAppConfig).mockRejectedValueOnce(new Error('全局配置格式不对：缺 default_entry_node_id'));

    expect(await readGlobalConfig(scope.begin('config'))).toBe(false);
    expect(useSettingsStore.getState()).toMatchObject({ globalConfigRead: { loaded: false, error: '全局配置格式不对：缺 default_entry_node_id' }, defaultNodeId: '' });

    expect(await readGlobalConfig(scope.begin('config'))).toBe(true);
    expect(useSettingsStore.getState()).toMatchObject({ globalConfigRead: LOADED, defaultNodeId: 'main', globalModel: 'global-model' });
  });

  it('已经作废的读取成败都不写', async () => {
    const scope = new RequestScope();
    const stale = scope.begin('read');
    scope.drop('read');
    vi.mocked(api.getNodes).mockRejectedValue(new Error('boom'));

    expect(await readNodeList('test-token', stale)).toBeNull();
    expect(await readActiveNode('session-1', stale)).toBeNull();
    expect(useSettingsStore.getState().nodeListRead).toBe(UNREAD);
    expect(useSettingsStore.getState().activeNodeSessionId).toBe('');
  });
});
