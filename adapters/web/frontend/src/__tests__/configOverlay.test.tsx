// 配置覆盖层：仓库默认直接生效，本实例改过才在 config/ 下存一份。
// 删这份是「恢复默认」，删自己加的才是真删；没改过的默认和系统的删不动。
// 三个页面共用这套说法，删错一次就是把别人的东西弄没了，所以逐页盯住按钮、确认框和删完的选中项。
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import type { AdminNode, AdminTool, NodeFileInfo } from '../api/supervisorClient';
import { AgentsSettingsPage } from '../components/settings/pages/AgentsSettingsPage';
import { NodeFilesSettingsPage } from '../components/settings/pages/NodeFilesSettingsPage';
import { ToolsSettingsPage } from '../components/settings/pages/ToolsSettingsPage';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

// 工具页同屏还挂着另外三块，各自去拉自己的数据，这里只看工具清单那一块。
vi.mock('../components/settings/pages/NodeGrantsSection', () => ({ NodeGrantsSection: () => null }));
vi.mock('../components/settings/pages/PolicyRulesSection', () => ({ PolicyRulesSection: () => null }));
vi.mock('../components/settings/pages/AutoApproveSection', () => ({ AutoApproveSection: () => null }));

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  createNode: vi.fn(),
  createTool: vi.fn(),
  deleteNode: vi.fn(),
  deleteNodeFile: vi.fn(),
  deleteTool: vi.fn(),
  getNodeFileRaw: vi.fn(),
  getNodeFiles: vi.fn(),
  getNodes: vi.fn(),
  getTools: vi.fn(),
  reloadTools: vi.fn(),
  updateNodeFileRaw: vi.fn(),
}));

// 接口那几个包装函数要打真 fetch，被 mock 的那份在这里用不上。
const client = await vi.importActual<typeof import('../api/supervisorClient')>('../api/supervisorClient');

const TOKEN = 'admin-token';

const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }));

const button = (name: string) => screen.getByRole('button', { name });

const statusText = (): string => screen.getByRole('status').textContent || '';

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function stubFetch(body: unknown) {
  const fetchMock = vi.fn(async () => jsonResponse(body));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('覆盖层接口', () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it('删节点文件走 DELETE，回的是删完东西还在不在', async () => {
    const fetchMock = stubFetch({ ok: true, restored: true });

    await expect(client.deleteNodeFile(TOKEN, 'chat/intent.yaml')).resolves.toEqual({ ok: true, restored: true });

    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/node-files/chat%2Fintent.yaml', {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
  });

  it('删节点和删工具也回同一种结果', async () => {
    const fetchMock = stubFetch({ ok: true, restored: false });

    await expect(client.deleteNode(TOKEN, 'chat.intent')).resolves.toEqual({ ok: true, restored: false });
    await expect(client.deleteTool(TOKEN, 'demo_tool')).resolves.toEqual({ ok: true, restored: false });

    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/nodes/chat.intent', expect.objectContaining({ method: 'DELETE' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/tools/demo_tool', expect.objectContaining({ method: 'DELETE' }));
  });

  it('读 runtime 连来源和路径一起读回来', async () => {
    const fetchMock = stubFetch({ content: 'engine: {}\n', source: 'override', path: 'config/runtime.yaml' });

    await expect(client.getRuntimeFile(TOKEN)).resolves.toEqual({
      content: 'engine: {}\n', hash: null, source: 'override', path: 'config/runtime.yaml',
    });

    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/runtime/raw', {
      headers: { Authorization: `Bearer ${TOKEN}` },
      signal: expect.any(AbortSignal),
    });
  });

  it('后端没说来源就当仓库默认，缺的字段补空串', async () => {
    stubFetch({});

    await expect(client.getRuntimeFile(TOKEN)).resolves.toEqual({ content: '', hash: null, source: 'default', path: '' });
  });

  it('runtime 恢复默认删的是同一个地址', async () => {
    const fetchMock = stubFetch({ ok: true, restored: true });

    await expect(client.resetRuntime(TOKEN)).resolves.toEqual({ ok: true, restored: true });

    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/runtime/raw', {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
  });

  it('读 runtime 原文的老包装还在，正文连指纹一起给', async () => {
    stubFetch({ content: 'engine: {}\n', hash: 'runtime-1', source: 'override' });

    await expect(client.getRuntimeRaw(TOKEN)).resolves.toEqual({ content: 'engine: {}\n', hash: 'runtime-1' });
  });
});

const nodeFile = (name: string, over: Partial<NodeFileInfo> = {}): NodeFileInfo => ({
  name,
  path: `config/nodes/${name}`,
  kind: 'node',
  suffix: '.yaml',
  is_example: false,
  base_name: name.replace(/\.yaml$/, ''),
  size: 1,
  updated_at: 0,
  source: 'custom',
  same_as_default: false,
  ...over,
});

const FILES: NodeFileInfo[] = [
  nodeFile('bundled.yaml', { source: 'default' }),
  nodeFile('edited.yaml', { source: 'override' }),
  nodeFile('mine.yaml', { source: 'custom' }),
  nodeFile('same.yaml', { source: 'override', same_as_default: true }),
];

const SAME_AS_DEFAULT_HINT = '这份和仓库默认一字不差，什么也没改，只会挡住以后仓库对它的更新，建议恢复默认。';

describe('节点文件的来源与删除', () => {
  /** 换文件，并等它的正文读回来。 */
  const selectFile = async (name: string) => {
    fireEvent.change(screen.getByLabelText('当前文件'), { target: { value: name } });
    await waitFor(() => expect(screen.getByLabelText('文件内容')).toHaveValue(`# ${name}`));
  };

  const fileSelect = () => screen.getByLabelText('当前文件') as HTMLSelectElement;

  const optionNames = () => [...fileSelect().options].map((option) => option.textContent);

  const ready = async () => {
    render(<NodeFilesSettingsPage />);
    await waitFor(() => expect(screen.getByLabelText('文件内容')).toHaveValue('# bundled.yaml'));
  };

  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    useSettingsStore.setState({ adminToken: TOKEN, isAuthenticated: true });
    vi.mocked(api.getNodeFiles).mockResolvedValue(FILES);
    vi.mocked(api.getNodeFileRaw).mockImplementation(async (_token, name) => ({ content: `# ${name}`, hash: `${name}-1` }));
    vi.mocked(api.updateNodeFileRaw).mockResolvedValue({ ok: true, hash: 'saved' });
    vi.mocked(api.deleteNodeFile).mockResolvedValue({ ok: true, restored: true });
  });

  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('保存交回这份文件加载时的指纹，存完换成结果里的新指纹', async () => {
    await ready();
    // 自建的那份保存后不重读列表，才看得出底子换的是保存结果里那一版。
    await selectFile('mine.yaml');

    fireEvent.change(screen.getByLabelText('文件内容'), { target: { value: '# 改过' } });
    click('保存当前文件');
    await waitFor(() => expect(api.updateNodeFileRaw).toHaveBeenCalledOnce());
    fireEvent.change(screen.getByLabelText('文件内容'), { target: { value: '# 再改一次' } });
    click('保存当前文件');
    await waitFor(() => expect(api.updateNodeFileRaw).toHaveBeenCalledTimes(2));

    expect(vi.mocked(api.updateNodeFileRaw).mock.calls.map(call => [call[1], call[3]]))
      .toEqual([['mine.yaml', 'mine.yaml-1'], ['mine.yaml', 'saved']]);
  });

  it('换一份文件就换一份底子，不拿上一份的指纹去存这一份', async () => {
    await ready();

    await selectFile('mine.yaml');
    fireEvent.change(screen.getByLabelText('文件内容'), { target: { value: '# 改过' } });
    click('保存当前文件');

    await waitFor(() => expect(api.updateNodeFileRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateNodeFileRaw).mock.calls[0])
      .toEqual([TOKEN, 'mine.yaml', '# 改过', 'mine.yaml-1']);
  });

  it('每个选项都带来源，路径那行也写着', async () => {
    await ready();

    expect(optionNames()).toEqual([
      'bundled.yaml · 仓库默认',
      'edited.yaml · 改过',
      'mine.yaml · 自建',
      'same.yaml · 改过 · 与默认相同',
    ]);
    expect(screen.getByText('config/nodes/bundled.yaml · 节点 YAML · 1 bytes · 仓库默认')).toBeInTheDocument();

    await selectFile('mine.yaml');
    expect(screen.getByText('config/nodes/mine.yaml · 节点 YAML · 1 bytes · 自建')).toBeInTheDocument();
  });

  it('改过却和默认一字不差的，劝一句恢复默认', async () => {
    await ready();
    expect(screen.queryByText(SAME_AS_DEFAULT_HINT)).toBeNull();

    await selectFile('same.yaml');

    expect(screen.getByText(SAME_AS_DEFAULT_HINT)).toBeInTheDocument();
  });

  it('「本实例改过或自建的」把没动过的仓库默认筛掉', async () => {
    await ready();

    fireEvent.change(screen.getByLabelText('文件类型过滤'), { target: { value: 'changed' } });

    // 选中的那份不在过滤结果里也留一条，不然正在编辑的文件会从选择框里消失。
    expect(optionNames()).toEqual([
      'bundled.yaml · 当前编辑',
      'edited.yaml · 改过',
      'mine.yaml · 自建',
      'same.yaml · 改过 · 与默认相同',
    ]);
  });

  it('没改过的仓库默认没有本实例的文件可删，按钮点不动', async () => {
    await ready();

    expect(button('恢复默认')).toBeDisabled();
    expect(screen.queryByRole('button', { name: '删除' })).toBeNull();
  });

  it('取消确认就什么都不删', async () => {
    await ready();
    await selectFile('mine.yaml');
    vi.mocked(window.confirm).mockReturnValue(false);

    click('删除');

    expect(window.confirm).toHaveBeenCalledWith('确定删除文件 mine.yaml吗？对应的文件会被删掉。');
    expect(api.deleteNodeFile).not.toHaveBeenCalled();
    expect(fileSelect()).toHaveValue('mine.yaml');
  });

  it('有未保存修改时确认里先说清楚会一起丢掉', async () => {
    await ready();
    await selectFile('mine.yaml');
    fireEvent.change(screen.getByLabelText('文件内容'), { target: { value: '# 改了一半' } });

    click('删除');
    await waitFor(() => expect(api.deleteNodeFile).toHaveBeenCalled());

    expect(window.confirm).toHaveBeenCalledWith('确定删除文件 mine.yaml吗？对应的文件会被删掉。当前还有未保存修改，也会一起丢掉。');
  });

  it('恢复默认之后还选着它，来源换成仓库默认', async () => {
    await ready();
    await selectFile('edited.yaml');
    vi.mocked(api.getNodeFiles).mockResolvedValue(FILES.map((file) => (
      file.name === 'edited.yaml' ? { ...file, source: 'default' as const } : file
    )));

    click('恢复默认');
    await waitFor(() => expect(statusText()).toBe('文件 edited.yaml已恢复成仓库默认'));

    expect(window.confirm).toHaveBeenCalledWith('把文件 edited.yaml恢复成仓库默认？本实例对它的修改会删掉。');
    expect(api.deleteNodeFile).toHaveBeenCalledWith(TOKEN, 'edited.yaml');
    expect(fileSelect()).toHaveValue('edited.yaml');
    expect(screen.getByText('config/nodes/edited.yaml · 节点 YAML · 1 bytes · 仓库默认')).toBeInTheDocument();
    expect(button('恢复默认')).toBeDisabled();
  });

  it('删掉自建的就不再选着它，列表里也没了', async () => {
    await ready();
    await selectFile('mine.yaml');
    vi.mocked(api.deleteNodeFile).mockResolvedValue({ ok: true, restored: false });
    vi.mocked(api.getNodeFiles).mockResolvedValue(FILES.filter((file) => file.name !== 'mine.yaml'));

    click('删除');
    await waitFor(() => expect(statusText()).toBe('文件 mine.yaml已删除'));

    expect(api.deleteNodeFile).toHaveBeenCalledWith(TOKEN, 'mine.yaml');
    expect(fileSelect()).toHaveValue('bundled.yaml');
    expect(optionNames()).not.toContain('mine.yaml · 自建');
  });

  it('保存仓库默认那份会重读列表：它从此归本实例了', async () => {
    await ready();
    fireEvent.change(screen.getByLabelText('文件内容'), { target: { value: '# 本实例的改法' } });
    vi.mocked(api.getNodeFiles).mockResolvedValue(FILES.map((file) => (
      file.name === 'bundled.yaml' ? { ...file, source: 'override' as const, path: 'config/nodes/bundled.yaml' } : file
    )));

    click('保存当前文件');
    await waitFor(() => expect(statusText()).toBe('已保存 bundled.yaml'));

    expect(api.getNodeFiles).toHaveBeenCalledTimes(2);
    expect(button('恢复默认')).toBeEnabled();
  });

  it('保存本来就属于本实例的那份不用重读列表', async () => {
    await ready();
    await selectFile('mine.yaml');
    fireEvent.change(screen.getByLabelText('文件内容'), { target: { value: '# 又改了一版' } });

    click('保存当前文件');
    await waitFor(() => expect(statusText()).toBe('已保存 mine.yaml'));

    expect(api.getNodeFiles).toHaveBeenCalledTimes(1);
  });
});

const node = (id: string, over: Partial<AdminNode> = {}): AdminNode => ({
  id, type: 'ai', name: id, model: '', description: '', delegate_targets: [], ...over,
});

const NODES: AdminNode[] = [
  node('chat.bundled', { source: 'default' }),
  node('chat.edited', { source: 'override' }),
  node('chat.mine', { source: 'custom' }),
  node('system.compactor', { source: 'system' }),
];

describe('节点的来源与删除', () => {
  const nodeRow = (id: string) => screen.getByRole('button', { name: new RegExp(id.replace('.', '\\.')) });

  const selectNode = async (id: string) => {
    fireEvent.click(nodeRow(id));
    await waitFor(() => expect(useSettingsSelectionStore.getState().selectedNode?.id).toBe(id));
  };

  const ready = async () => {
    render(<AgentsSettingsPage />);
    await screen.findByText('chat.bundled');
  };

  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    useSettingsStore.setState({ adminToken: TOKEN, isAuthenticated: true, rightPanelOpen: false });
    useSettingsSelectionStore.setState({ selectedNode: null });
    vi.mocked(api.getNodes).mockResolvedValue(NODES);
    vi.mocked(api.deleteNode).mockResolvedValue({ ok: true, restored: true });
  });

  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('每个节点都挂一枚来源标', async () => {
    await ready();

    expect(within(nodeRow('chat.bundled')).getByText('仓库默认')).toBeInTheDocument();
    expect(within(nodeRow('chat.edited')).getByText('改过')).toBeInTheDocument();
    expect(within(nodeRow('chat.mine')).getByText('自建')).toBeInTheDocument();
    expect(within(nodeRow('system.compactor')).getByText('系统')).toBeInTheDocument();
  });

  it('没改过的仓库默认和系统节点都删不动', async () => {
    await ready();

    await selectNode('chat.bundled');
    expect(button('删除选中节点')).toBeDisabled();

    await selectNode('system.compactor');
    expect(button('删除选中节点')).toBeDisabled();
  });

  it('改过的节点按钮改口叫恢复默认，删完重新选中它', async () => {
    await ready();
    await selectNode('chat.edited');
    const restored = node('chat.edited', { source: 'default' });
    vi.mocked(api.getNodes).mockResolvedValue(NODES.map((item) => (item.id === 'chat.edited' ? restored : item)));

    click('恢复默认选中节点');
    await waitFor(() => expect(statusText()).toBe('节点 chat.edited已恢复成仓库默认'));

    expect(window.confirm).toHaveBeenCalledWith('把节点 chat.edited恢复成仓库默认？本实例对它的修改会删掉。');
    expect(api.deleteNode).toHaveBeenCalledWith(TOKEN, 'chat.edited');
    // 重选一次右栏才会把仓库那份读回来，选中的必须是重读之后的对象。
    expect(useSettingsSelectionStore.getState().selectedNode).toEqual(restored);
    expect(button('删除选中节点')).toBeDisabled();
  });

  it('自建节点是真删，删完不再选中任何节点', async () => {
    await ready();
    await selectNode('chat.mine');
    vi.mocked(api.deleteNode).mockResolvedValue({ ok: true, restored: false });
    vi.mocked(api.getNodes).mockResolvedValue(NODES.filter((item) => item.id !== 'chat.mine'));

    click('删除选中节点');
    await waitFor(() => expect(statusText()).toBe('节点 chat.mine已删除'));

    expect(useSettingsSelectionStore.getState().selectedNode).toBeNull();
    expect(screen.queryByText('chat.mine')).toBeNull();
  });

  it('取消确认就什么都不删，选中项也不动', async () => {
    await ready();
    await selectNode('chat.mine');
    vi.mocked(window.confirm).mockReturnValue(false);

    click('删除选中节点');
    await waitFor(() => expect(window.confirm).toHaveBeenCalledWith('确定删除节点 chat.mine吗？对应的文件会被删掉。'));

    expect(api.deleteNode).not.toHaveBeenCalled();
    expect(useSettingsSelectionStore.getState().selectedNode?.id).toBe('chat.mine');
  });

  it('老后端不给来源就当自建的，照旧能删', async () => {
    vi.mocked(api.getNodes).mockResolvedValue([node('legacy.node')]);
    render(<AgentsSettingsPage />);
    await screen.findByText('legacy.node');
    fireEvent.click(screen.getByRole('button', { name: /legacy\.node/ }));

    await waitFor(() => expect(button('删除选中节点')).toBeEnabled());
    expect(screen.queryByRole('button', { name: '恢复默认选中节点' })).toBeNull();
  });
});

const tool = (name: string, over: Partial<AdminTool> = {}): AdminTool => ({
  name, source: 'external', editable: true, description: `${name} 的说明`, input_schema: {}, timeout_sec: 30, has_spec: true, ...over,
});

const TOOLS: AdminTool[] = [
  tool('bundled_tool', { origin: 'bundled' }),
  tool('edited_tool', { origin: 'override' }),
  tool('mine_tool', { origin: 'custom' }),
  tool('read_file', { source: 'builtin', editable: false }),
];

describe('工具的来源与删除', () => {
  const toolRow = (name: string) => screen.getByRole('button', { name: new RegExp(name) });

  const selectTool = async (name: string) => {
    fireEvent.click(toolRow(name));
    await waitFor(() => expect(useSettingsSelectionStore.getState().selectedTool?.name).toBe(name));
  };

  const ready = async () => {
    render(<ToolsSettingsPage />);
    await screen.findByRole('button', { name: /bundled_tool/ });
  };

  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    useSettingsStore.setState({ adminToken: TOKEN, isAuthenticated: true, rightPanelOpen: false });
    useSettingsSelectionStore.setState({ selectedTool: null, allToolNames: [] });
    vi.mocked(api.getTools).mockResolvedValue(TOOLS);
    vi.mocked(api.deleteTool).mockResolvedValue({ ok: true, restored: true });
  });

  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('外部脚本标出从哪来，内置的不掺和', async () => {
    await ready();

    expect(within(toolRow('bundled_tool')).getByText('仓库自带')).toBeInTheDocument();
    expect(within(toolRow('edited_tool')).getByText('改过')).toBeInTheDocument();
    expect(within(toolRow('mine_tool')).getByText('自建')).toBeInTheDocument();
    for (const label of ['仓库自带', '改过', '自建']) {
      expect(within(toolRow('read_file')).queryByText(label)).toBeNull();
    }
  });

  it('没改过的仓库自带工具删不动', async () => {
    await ready();
    await selectTool('bundled_tool');

    expect(button('删除选中工具')).toBeDisabled();
  });

  it('改过的工具是恢复默认，删完重新选中它', async () => {
    await ready();
    await selectTool('edited_tool');
    const restored = tool('edited_tool', { origin: 'bundled' });
    vi.mocked(api.getTools).mockResolvedValue(TOOLS.map((item) => (item.name === 'edited_tool' ? restored : item)));

    click('恢复默认选中工具');
    await waitFor(() => expect(statusText()).toBe('工具 edited_tool已恢复成仓库默认，重载工具后生效'));

    expect(window.confirm).toHaveBeenCalledWith('把工具 edited_tool恢复成仓库默认？本实例对它的修改会删掉。');
    expect(api.deleteTool).toHaveBeenCalledWith(TOKEN, 'edited_tool');
    expect(useSettingsSelectionStore.getState().selectedTool).toEqual(restored);
    expect(button('删除选中工具')).toBeDisabled();
  });

  it('自建工具是真删，删完不再选中任何工具', async () => {
    await ready();
    await selectTool('mine_tool');
    vi.mocked(api.deleteTool).mockResolvedValue({ ok: true, restored: false });
    vi.mocked(api.getTools).mockResolvedValue(TOOLS.filter((item) => item.name !== 'mine_tool'));

    click('删除选中工具');
    await waitFor(() => expect(statusText()).toBe('工具 mine_tool已删除，重载工具后生效'));

    expect(useSettingsSelectionStore.getState().selectedTool).toBeNull();
    expect(screen.queryByRole('button', { name: /mine_tool/ })).toBeNull();
  });

  it('取消确认就什么都不删', async () => {
    await ready();
    await selectTool('mine_tool');
    vi.mocked(window.confirm).mockReturnValue(false);

    click('删除选中工具');
    await waitFor(() => expect(window.confirm).toHaveBeenCalledWith('确定删除工具 mine_tool吗？对应的文件会被删掉。'));

    expect(api.deleteTool).not.toHaveBeenCalled();
    expect(useSettingsSelectionStore.getState().selectedTool?.name).toBe('mine_tool');
  });

  it('老后端不给来源就当自建的，照旧能删', async () => {
    vi.mocked(api.getTools).mockResolvedValue([tool('legacy_tool')]);
    render(<ToolsSettingsPage />);
    await screen.findByRole('button', { name: /legacy_tool/ });
    fireEvent.click(screen.getByRole('button', { name: /legacy_tool/ }));

    await waitFor(() => expect(button('删除选中工具')).toBeEnabled());
    expect(screen.queryByRole('button', { name: '恢复默认选中工具' })).toBeNull();
  });
});
