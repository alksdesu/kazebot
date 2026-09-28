// 技能 / 工具 / 定时任务 / MCP / 节点五个列表页的提示：写操作紧接着就重读列表，
// 刚写下的那句不能被这一次重读冲掉，也不能被右栏保存引起的后台重读冲掉；
// 重读失败时原因接在它后面，下一次读成功只去掉原因，只有点「刷新」两样一起清；
// 页面只认最后发出的那次读，写完发现 token 换了就不再替旧会话收尾。
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentType } from 'react';
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import * as api from '../api/supervisorClient';
import type { AdminNode, AdminSkill, AdminTool, McpClient } from '../api/supervisorClient';
import { AgentsSettingsPage } from '../components/settings/pages/AgentsSettingsPage';
import { AutomationSettingsPage } from '../components/settings/pages/AutomationSettingsPage';
import { McpSettingsPage } from '../components/settings/pages/McpSettingsPage';
import { SkillsSettingsPage } from '../components/settings/pages/SkillsSettingsPage';
import { ToolsSettingsPage } from '../components/settings/pages/ToolsSettingsPage';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

// 工具页同屏还挂着另外三块，各自去拉自己的数据，这里只看工具清单那一块的提示。
vi.mock('../components/settings/pages/NodeGrantsSection', () => ({ NodeGrantsSection: () => null }));
vi.mock('../components/settings/pages/PolicyRulesSection', () => ({ PolicyRulesSection: () => null }));
vi.mock('../components/settings/pages/AutoApproveSection', () => ({ AutoApproveSection: () => null }));

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  createNode: vi.fn(),
  createSkill: vi.fn(),
  createTool: vi.fn(),
  deleteNode: vi.fn(),
  deleteSkill: vi.fn(),
  deleteTool: vi.fn(),
  getMcpClients: vi.fn(),
  getMcpClientsRaw: vi.fn(),
  getNodes: vi.fn(),
  getSchedulesRaw: vi.fn(),
  getSkills: vi.fn(),
  getTools: vi.fn(),
  reloadTools: vi.fn(),
  updateMcpClientsRaw: vi.fn(),
  updateSchedulesRaw: vi.fn(),
}));

/** 没有消息时整个 StatusText 不渲染，只能等它出现。 */
const statusText = async (): Promise<string> => (await screen.findByRole('status')).textContent || '';

const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }));

const fill = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

/** 写操作和它带出来的那次重读都跑完了：刷新按钮既不在读也不在写，才重新可点。 */
const settled = (refreshLabel: string) => waitFor(() => expect(screen.getByRole('button', { name: refreshLabel })).toBeEnabled());

const expectCleared = async (refreshLabel: string) => {
  click(refreshLabel);
  await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
};

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

/** 手动控制一次请求什么时候回来：版本守卫和换 token 都要卡在请求返回之前。 */
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const readsWith = (reader: Mock, token: string): number => reader.mock.calls.filter((call) => call[0] === token).length;

const READ_FAILURE = '503 列表读不回来';

const WRITE_FAILURE = '502 写不进去';

const ADMIN_TOKEN = 'admin-token';

const ROTATED_TOKEN = 'rotated-token';

const EXISTING_SKILL: AdminSkill = { name: 'existing_skill', enabled: true, strategy: 'normal', keywords: [] };

const EXISTING_TOOL: AdminTool = {
  name: 'existing_tool', source: 'external', editable: true, description: '已有的外部工具。',
  input_schema: { type: 'object', properties: {} }, timeout_sec: 30, has_spec: true,
};

const EXISTING_NODE = {
  id: 'existing_node', name: 'existing_node', type: 'ai', model: '', description: '已有的节点。', delegate_targets: [],
} as AdminNode;

const EXISTING_CLIENT: McpClient = { id: 'existing_client', enabled: false, transport: 'streamable_http' };

const SCHEDULES_RAW = [
  'schedules:',
  '  - id: existing_schedule',
  '    cron: 0 0 * * *',
  '    type: message',
  "    text: ''",
  '    enabled: false',
  '',
].join('\n');

const MCP_RAW = [
  'version: 1',
  'clients:',
  '  existing_client:',
  '    transport: streamable_http',
  '    enabled: false',
  "    url: ''",
  '',
].join('\n');

// 过时那次读回来的东西：名字和现在列表里的不一样，覆盖没覆盖一眼就看得出来。
const STALE_SKILL: AdminSkill = { name: 'stale_skill', enabled: true, strategy: 'normal', keywords: [] };

const STALE_TOOL: AdminTool = { ...EXISTING_TOOL, name: 'stale_tool' };

const STALE_NODE = { ...EXISTING_NODE, id: 'stale_node', name: 'stale_node' } as AdminNode;

const STALE_CLIENT: McpClient = { id: 'stale_client', enabled: false, transport: 'streamable_http' };

const STALE_SCHEDULES_RAW = SCHEDULES_RAW.replace('existing_schedule', 'stale_schedule');

/** 定时任务和 MCP 这两页没有增删接口，新建和删除都得先把整份原文读回来再整份写回去。 */
interface RawCase {
  /** 读原文那个接口，和列表重读的不一定是同一个。 */
  reader: Mock;
  /** 读回来的原文。 */
  value: string;
  /** 用原文里已有的那个名字去创建。 */
  createExisting: () => void;
  existsText: string;
}

interface PageCase {
  /** 用例标题里的页面名。 */
  title: string;
  Page: ComponentType;
  /** 列表重读走的那个接口。 */
  reader: Mock;
  /** 首读之后再读一次会拿到的东西，用来铺失败前的那几次成功。 */
  listValue: unknown;
  /** 一次写操作总共引起几次列表重读：有的页要先读一遍原文才能改。 */
  writeReads: number;
  /** 其中落在写之前的那几次：换 token 的用例只该看到这些用旧 token 读的。 */
  preReads: number;
  /** 列表里认得出这份数据的那个名字。 */
  listName: string;
  /** 过时那次读会拿回来的东西，以及它在列表里的名字。 */
  staleValue: unknown;
  staleName: string;
  /** 右栏保存之后广播的事件名。 */
  event: string;
  refreshLabel: string;
  create: () => void;
  createWriter: Mock;
  createdText: string;
  /** 造一个选中项，删除按钮才点得动。 */
  select: () => void;
  /** 当前选中项，换 token 的用例要确认它没被动过。 */
  selection: () => unknown;
  deleteLabel: string;
  deleteWriter: Mock;
  deletedText: string;
  /** 新建走完落在重读之后的那一步：选中项换成刚建的，右栏开不开各页不同。 */
  created: {
    selection: unknown;
    panelOpen: boolean;
  };
  raw?: RawCase;
}

const PAGES: PageCase[] = [
  {
    title: '技能页',
    Page: SkillsSettingsPage,
    reader: vi.mocked(api.getSkills),
    listValue: [EXISTING_SKILL],
    writeReads: 1,
    preReads: 0,
    listName: EXISTING_SKILL.name,
    staleValue: [STALE_SKILL],
    staleName: STALE_SKILL.name,
    event: 'settings:skills-updated',
    refreshLabel: '刷新技能',
    create: () => { fill('创建技能', 'demo_skill'); click('创建技能'); },
    createWriter: vi.mocked(api.createSkill),
    createdText: '技能已创建，请在右栏编辑 frontmatter 和正文。',
    select: () => useSettingsSelectionStore.setState({ selectedSkill: EXISTING_SKILL }),
    selection: () => useSettingsSelectionStore.getState().selectedSkill,
    deleteLabel: '删除选中技能',
    deleteWriter: vi.mocked(api.deleteSkill),
    deletedText: '技能已删除',
    created: {
      selection: { name: 'demo_skill', description: '通过设置页面创建的技能。', enabled: true, strategy: 'normal', keywords: [] },
      panelOpen: false,
    },
  },
  {
    title: '工具页',
    Page: ToolsSettingsPage,
    reader: vi.mocked(api.getTools),
    listValue: [EXISTING_TOOL],
    writeReads: 1,
    preReads: 0,
    listName: EXISTING_TOOL.name,
    staleValue: [STALE_TOOL],
    staleName: STALE_TOOL.name,
    event: 'settings:tools-updated',
    refreshLabel: '刷新工具',
    create: () => { fill('创建工具', 'demo_tool'); click('创建工具'); },
    createWriter: vi.mocked(api.createTool),
    createdText: '工具已创建，请在右栏编辑 Python 脚本。',
    select: () => useSettingsSelectionStore.setState({ selectedTool: EXISTING_TOOL }),
    selection: () => useSettingsSelectionStore.getState().selectedTool,
    deleteLabel: '删除选中工具',
    deleteWriter: vi.mocked(api.deleteTool),
    deletedText: '工具已删除',
    created: {
      selection: {
        name: 'demo_tool', source: 'external', editable: true, description: '通过设置页面创建的工具。',
        input_schema: { type: 'object', properties: {} }, timeout_sec: 30, has_spec: true,
      },
      panelOpen: false,
    },
  },
  {
    title: '定时任务页',
    Page: AutomationSettingsPage,
    reader: vi.mocked(api.getSchedulesRaw),
    listValue: SCHEDULES_RAW,
    writeReads: 2,
    preReads: 1,
    listName: 'existing_schedule',
    staleValue: STALE_SCHEDULES_RAW,
    staleName: 'stale_schedule',
    event: 'settings:schedules-updated',
    refreshLabel: '刷新',
    create: () => { fill('创建定时任务', 'demo_schedule'); click('创建任务'); },
    createWriter: vi.mocked(api.updateSchedulesRaw),
    createdText: '任务已创建但尚未启用，请在右栏填写内容后启用。',
    select: () => useSettingsSelectionStore.setState({ selectedScheduleId: 'existing_schedule' }),
    selection: () => useSettingsSelectionStore.getState().selectedScheduleId,
    deleteLabel: '删除选中任务',
    deleteWriter: vi.mocked(api.updateSchedulesRaw),
    deletedText: '定时任务已删除',
    created: { selection: 'demo_schedule', panelOpen: true },
    raw: {
      reader: vi.mocked(api.getSchedulesRaw),
      value: SCHEDULES_RAW,
      createExisting: () => { fill('创建定时任务', 'existing_schedule'); click('创建任务'); },
      existsText: '该任务 ID 已存在',
    },
  },
  {
    title: 'MCP 页',
    Page: McpSettingsPage,
    reader: vi.mocked(api.getMcpClients),
    listValue: [EXISTING_CLIENT],
    writeReads: 1,
    preReads: 0,
    listName: EXISTING_CLIENT.id,
    staleValue: [STALE_CLIENT],
    staleName: STALE_CLIENT.id,
    event: 'settings:mcp-updated',
    refreshLabel: '刷新 MCP',
    create: () => { fill('创建 MCP Client', 'demo_client'); click('创建 Client'); },
    createWriter: vi.mocked(api.updateMcpClientsRaw),
    createdText: 'Client 已创建但尚未启用，请在右栏填写连接信息后启用。',
    select: () => useSettingsSelectionStore.setState({ selectedMcpClient: EXISTING_CLIENT }),
    selection: () => useSettingsSelectionStore.getState().selectedMcpClient,
    deleteLabel: '删除选中 Client',
    deleteWriter: vi.mocked(api.updateMcpClientsRaw),
    deletedText: 'MCP Client 已删除',
    created: {
      selection: { id: 'demo_client', enabled: false, transport: 'streamable_http', description: '通过设置页面创建的 MCP Client。' },
      panelOpen: true,
    },
    raw: {
      reader: vi.mocked(api.getMcpClientsRaw),
      value: MCP_RAW,
      createExisting: () => { fill('创建 MCP Client', 'existing_client'); click('创建 Client'); },
      existsText: '该 MCP Client 已存在',
    },
  },
  {
    title: '节点页',
    Page: AgentsSettingsPage,
    reader: vi.mocked(api.getNodes),
    listValue: [EXISTING_NODE],
    writeReads: 1,
    preReads: 0,
    listName: EXISTING_NODE.id,
    staleValue: [STALE_NODE],
    staleName: STALE_NODE.id,
    event: 'settings:nodes-updated',
    refreshLabel: '刷新节点',
    create: () => { fill('创建节点', 'demo_node'); click('创建节点'); },
    createWriter: vi.mocked(api.createNode),
    createdText: '节点已创建，请在右栏继续编辑 YAML。',
    select: () => useSettingsSelectionStore.setState({ selectedNode: EXISTING_NODE }),
    selection: () => useSettingsSelectionStore.getState().selectedNode,
    deleteLabel: '删除选中节点',
    deleteWriter: vi.mocked(api.deleteNode),
    deletedText: '节点已删除',
    created: {
      selection: { id: 'demo_node', name: 'demo_node', type: 'ai', model: '', description: '通过设置页面创建的节点。', delegate_targets: [] },
      panelOpen: false,
    },
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: ADMIN_TOKEN, isAuthenticated: true, rightPanelOpen: false });
  useSettingsSelectionStore.setState({
    selectedNode: null, selectedTool: null, selectedSkill: null, selectedScheduleId: null, selectedMcpClient: null,
    allToolNames: [],
  });
  vi.mocked(api.getNodes).mockResolvedValue([EXISTING_NODE]);
  vi.mocked(api.getSkills).mockResolvedValue([EXISTING_SKILL]);
  vi.mocked(api.getTools).mockResolvedValue([EXISTING_TOOL]);
  vi.mocked(api.getMcpClients).mockResolvedValue([EXISTING_CLIENT]);
  vi.mocked(api.getMcpClientsRaw).mockResolvedValue(MCP_RAW);
  vi.mocked(api.getSchedulesRaw).mockResolvedValue(SCHEDULES_RAW);
  vi.mocked(api.createNode).mockResolvedValue({ ok: true });
  vi.mocked(api.createSkill).mockResolvedValue({ ok: true });
  vi.mocked(api.createTool).mockResolvedValue({ ok: true });
  vi.mocked(api.deleteNode).mockResolvedValue({ ok: true });
  vi.mocked(api.deleteSkill).mockResolvedValue({ ok: true });
  vi.mocked(api.deleteTool).mockResolvedValue({ ok: true });
  vi.mocked(api.reloadTools).mockResolvedValue({ seq: 7 });
  vi.mocked(api.updateMcpClientsRaw).mockResolvedValue({ ok: true });
  vi.mocked(api.updateSchedulesRaw).mockResolvedValue({ ok: true });
});

for (const page of PAGES) describe(`${page.title}的提示`, () => {
  const { Page } = page;

  /** 首读跑完，写操作按钮才点得动。 */
  const ready = async () => {
    await waitFor(() => expect(page.reader).toHaveBeenCalledOnce());
    await settled(page.refreshLabel);
  };

  /** 写操作连同它带出来的那次重读都落地了。 */
  const written = async () => {
    await waitFor(() => expect(page.reader).toHaveBeenCalledTimes(1 + page.writeReads));
    await settled(page.refreshLabel);
  };

  /** 首读和写操作路上那几次读照常给，留下最后那次重读单独安排。 */
  const upToWriteReload = () => { for (let i = 0; i < page.writeReads; i += 1) page.reader.mockResolvedValueOnce(page.listValue); };

  /** 写操作那次重读失败。 */
  const failWriteReload = () => {
    upToWriteReload();
    page.reader.mockRejectedValueOnce(new Error(READ_FAILURE));
  };

  // 定时任务页读原文和重读是同一个接口，卡住重读得先把前面那几次读排开。
  const holdWriteReload = (): Deferred<unknown> => {
    const reload = deferred<unknown>();
    upToWriteReload();
    page.reader.mockReturnValueOnce(reload.promise);
    return reload;
  };

  /** 右栏保存之后广播的那个事件，页面收到就在后台重读一次。 */
  const backgroundReload = async () => {
    act(() => { window.dispatchEvent(new Event(page.event)); });
    await waitFor(() => expect(page.reader).toHaveBeenCalledTimes(2 + page.writeReads));
    await settled(page.refreshLabel);
  };

  /** 首读挂着不返回，后台那次先读回来：首读此后怎么落地都已经过时。 */
  const staleFirstRead = async (): Promise<Deferred<unknown>> => {
    const stale = deferred<unknown>();
    page.reader.mockReturnValueOnce(stale.promise);
    render(<Page />);
    await waitFor(() => expect(page.reader).toHaveBeenCalledOnce());

    act(() => { window.dispatchEvent(new Event(page.event)); });
    await waitFor(() => expect(screen.getByText(page.listName)).toBeInTheDocument());
    return stale;
  };

  /** 写接口卡住不返回，好在它返回之前把 token 换掉。 */
  const holdWrite = (writer: Mock): Deferred<unknown> => {
    const write = deferred<unknown>();
    writer.mockReturnValueOnce(write.promise);
    return write;
  };

  const rotateThenFinish = async (writer: Mock, write: Deferred<unknown>) => {
    await waitFor(() => expect(writer).toHaveBeenCalledOnce());
    await act(async () => {
      useSettingsStore.setState({ adminToken: ROTATED_TOKEN });
      write.resolve({ ok: true });
    });
    await settled(page.refreshLabel);
  };

  /** 换 token，并等新会话自己那次重读发出去：提示已经清空，旧会话此后再写就看得见。 */
  const rotated = async () => {
    act(() => { useSettingsStore.setState({ adminToken: ROTATED_TOKEN }); });
    await waitFor(() => expect(readsWith(page.reader, ROTATED_TOKEN)).toBe(1));
  };

  /** 写接口拖到新会话已经就绪之后才失败。 */
  const rotateThenFail = async (writer: Mock, write: Deferred<unknown>) => {
    await waitFor(() => expect(writer).toHaveBeenCalledOnce());
    await rotated();
    await act(async () => { write.reject(new Error(WRITE_FAILURE)); });
    await settled(page.refreshLabel);
  };

  it('新建之后紧接着的重读不冲掉提示，点刷新才清', async () => {
    render(<Page />);
    await ready();

    page.create();
    await written();

    expect(await statusText()).toBe(page.createdText);
    await expectCleared(page.refreshLabel);
  });

  it('删除之后紧接着的重读不冲掉提示', async () => {
    page.select();
    render(<Page />);
    await ready();

    click(page.deleteLabel);
    await written();

    expect(await statusText()).toBe(page.deletedText);
  });

  it('右栏保存引起的后台重读不冲掉提示', async () => {
    render(<Page />);
    await ready();
    page.create();
    await written();

    await backgroundReload();

    expect(screen.getByRole('status').textContent).toBe(page.createdText);
  });

  it('重读失败时原因接在结果后面，下一次读成功只去掉原因', async () => {
    failWriteReload();
    render(<Page />);
    await ready();

    page.create();
    await written();
    expect(await statusText()).toBe(`${page.createdText}${READ_FAILURE}`);

    await backgroundReload();
    expect(screen.getByRole('status').textContent).toBe(page.createdText);
  });

  it('点刷新把结果和读失败的原因一起清掉', async () => {
    failWriteReload();
    render(<Page />);
    await ready();

    page.create();
    await written();
    expect(await statusText()).toBe(`${page.createdText}${READ_FAILURE}`);

    await expectCleared(page.refreshLabel);
  });

  it('换了 token 重新进页面，提示清空', async () => {
    render(<Page />);
    await ready();
    page.create();
    await written();
    expect(await statusText()).toBe(page.createdText);

    act(() => { useSettingsStore.setState({ adminToken: ROTATED_TOKEN }); });

    await waitFor(() => expect(page.reader).toHaveBeenCalledTimes(2 + page.writeReads));
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
  });

  it('过时那次读失败，不显示已经作废的原因', async () => {
    const stale = await staleFirstRead();

    await act(async () => { stale.reject(new Error(READ_FAILURE)); });

    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByText(page.listName)).toBeInTheDocument();
    await settled(page.refreshLabel);
  });

  it('过时那次读回来的列表，不覆盖后发那次的', async () => {
    const stale = await staleFirstRead();

    await act(async () => { stale.resolve(page.staleValue); });

    expect(screen.queryByText(page.staleName)).toBeNull();
    expect(screen.getByText(page.listName)).toBeInTheDocument();
    await settled(page.refreshLabel);
  });

  it('新建写完 token 已经换掉，不写提示也不用旧 token 重读', async () => {
    const write = holdWrite(page.createWriter);
    render(<Page />);
    await ready();

    page.create();
    await rotateThenFinish(page.createWriter, write);

    expect(screen.queryByRole('status')).toBeNull();
    expect(readsWith(page.reader, ADMIN_TOKEN)).toBe(1 + page.preReads);
    expect(page.selection()).toBeNull();
    expect(useSettingsStore.getState().rightPanelOpen).toBe(false);
  });

  it('删除写完 token 已经换掉，不动选中项也不用旧 token 重读', async () => {
    const write = holdWrite(page.deleteWriter);
    page.select();
    const selected = page.selection();
    render(<Page />);
    await ready();

    click(page.deleteLabel);
    await rotateThenFinish(page.deleteWriter, write);

    expect(screen.queryByRole('status')).toBeNull();
    expect(readsWith(page.reader, ADMIN_TOKEN)).toBe(1 + page.preReads);
    expect(page.selection()).toBe(selected);
  });

  it('新建失败时 token 已经换掉，不把失败原因写到新会话', async () => {
    const write = holdWrite(page.createWriter);
    render(<Page />);
    await ready();

    page.create();
    await rotateThenFail(page.createWriter, write);

    expect(screen.queryByText(WRITE_FAILURE)).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('删除失败时 token 已经换掉，不把失败原因写到新会话', async () => {
    const write = holdWrite(page.deleteWriter);
    page.select();
    render(<Page />);
    await ready();

    click(page.deleteLabel);
    await rotateThenFail(page.deleteWriter, write);

    expect(screen.queryByText(WRITE_FAILURE)).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('新建之后那次重读回来，选中项落到刚建的那个上', async () => {
    render(<Page />);
    await ready();

    page.create();
    await written();

    expect(page.selection()).toEqual(page.created.selection);
    expect(useSettingsStore.getState().rightPanelOpen).toBe(page.created.panelOpen);
  });

  it('新建之后那次重读回来时 token 已经换掉，不改选中项也不开右栏', async () => {
    const reload = holdWriteReload();
    render(<Page />);
    await ready();

    page.create();
    await waitFor(() => expect(page.reader).toHaveBeenCalledTimes(1 + page.writeReads));
    await rotated();
    await act(async () => { reload.resolve(page.listValue); });
    await settled(page.refreshLabel);

    expect(page.selection()).toBeNull();
    expect(useSettingsStore.getState().rightPanelOpen).toBe(false);
    expect(readsWith(page.reader, ADMIN_TOKEN)).toBe(2 + page.preReads);
    expect(screen.queryByRole('status')).toBeNull();
  });

  const raw = page.raw;
  if (raw) describe('先读原文再写回', () => {
    /** 原文卡在半路，写不写得下去全看它回来时 token 还在不在。 */
    const holdRawRead = async (start: () => void): Promise<Deferred<string>> => {
      const read = deferred<string>();
      const before = raw.reader.mock.calls.length;
      raw.reader.mockReturnValueOnce(read.promise);
      start();
      await waitFor(() => expect(raw.reader.mock.calls.length).toBe(before + 1));
      return read;
    };

    const finishRawRead = async (read: Deferred<string>) => {
      await act(async () => { read.resolve(raw.value); });
      await settled(page.refreshLabel);
    };

    it('新建同名的提前挡下来，不写回', async () => {
      render(<Page />);
      await ready();

      raw.createExisting();

      expect(await statusText()).toBe(raw.existsText);
      expect(page.createWriter).not.toHaveBeenCalled();
    });

    it('原文读回来时 token 已经换掉，同名也不提示已存在', async () => {
      render(<Page />);
      await ready();
      const read = await holdRawRead(raw.createExisting);

      await rotated();
      await finishRawRead(read);

      expect(screen.queryByText(raw.existsText)).toBeNull();
      expect(screen.queryByRole('status')).toBeNull();
      expect(page.createWriter).not.toHaveBeenCalled();
    });

    it('新建的原文读回来时 token 已经换掉，不写回也不改选中项', async () => {
      render(<Page />);
      await ready();
      const read = await holdRawRead(page.create);

      await rotated();
      await finishRawRead(read);

      expect(page.createWriter).not.toHaveBeenCalled();
      expect(page.selection()).toBeNull();
      expect(useSettingsStore.getState().rightPanelOpen).toBe(false);
      expect(screen.queryByRole('status')).toBeNull();
    });

    it('删除的原文读回来时 token 已经换掉，不写回也不动选中项', async () => {
      page.select();
      const selected = page.selection();
      render(<Page />);
      await ready();
      const read = await holdRawRead(() => click(page.deleteLabel));

      await rotated();
      await finishRawRead(read);

      expect(page.deleteWriter).not.toHaveBeenCalled();
      expect(page.selection()).toBe(selected);
      expect(screen.queryByRole('status')).toBeNull();
    });
  });
});

describe('工具页重载之后的提示', () => {
  it('重载的序号提示活得过紧接着的重读，点「刷新工具」才清掉', async () => {
    render(<ToolsSettingsPage />);
    await waitFor(() => expect(api.getTools).toHaveBeenCalledOnce());

    click('重载工具');

    await waitFor(() => expect(api.getTools).toHaveBeenCalledTimes(2));
    await settled('刷新工具');
    expect(await statusText()).toBe('工具已重载，序号 7');

    await expectCleared('刷新工具');
  });

  it('序号提示没有句号，接读失败原因时补一个', async () => {
    vi.mocked(api.getTools).mockResolvedValueOnce([EXISTING_TOOL]).mockRejectedValueOnce(new Error(READ_FAILURE));
    render(<ToolsSettingsPage />);
    await waitFor(() => expect(api.getTools).toHaveBeenCalledOnce());

    click('重载工具');

    await waitFor(() => expect(api.getTools).toHaveBeenCalledTimes(2));
    await settled('刷新工具');
    expect(await statusText()).toBe(`工具已重载，序号 7。${READ_FAILURE}`);
  });

  it('重载完 token 已经换掉，不写序号也不用旧 token 重读', async () => {
    const write = deferred<{ seq: number }>();
    vi.mocked(api.reloadTools).mockReturnValueOnce(write.promise);
    render(<ToolsSettingsPage />);
    await waitFor(() => expect(api.getTools).toHaveBeenCalledOnce());
    await settled('刷新工具');

    click('重载工具');
    await waitFor(() => expect(api.reloadTools).toHaveBeenCalledOnce());
    await act(async () => {
      useSettingsStore.setState({ adminToken: ROTATED_TOKEN });
      write.resolve({ seq: 7 });
    });
    await settled('刷新工具');

    expect(screen.queryByRole('status')).toBeNull();
    expect(readsWith(vi.mocked(api.getTools), ADMIN_TOKEN)).toBe(1);
  });

  it('重载失败时 token 已经换掉，不把失败原因写到新会话', async () => {
    const write = deferred<{ seq: number }>();
    vi.mocked(api.reloadTools).mockReturnValueOnce(write.promise);
    render(<ToolsSettingsPage />);
    await waitFor(() => expect(api.getTools).toHaveBeenCalledOnce());
    await settled('刷新工具');

    click('重载工具');
    await waitFor(() => expect(api.reloadTools).toHaveBeenCalledOnce());
    act(() => { useSettingsStore.setState({ adminToken: ROTATED_TOKEN }); });
    await waitFor(() => expect(readsWith(vi.mocked(api.getTools), ROTATED_TOKEN)).toBe(1));
    await act(async () => { write.reject(new Error(WRITE_FAILURE)); });
    await settled('刷新工具');

    expect(screen.queryByText(WRITE_FAILURE)).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });
});

describe('工具清单给授权页的那份名单', () => {
  it('过时那次读回来的名单不覆盖后发那次的', async () => {
    const stale = deferred<AdminTool[]>();
    vi.mocked(api.getTools).mockReturnValueOnce(stale.promise);
    render(<ToolsSettingsPage />);
    await waitFor(() => expect(api.getTools).toHaveBeenCalledOnce());

    act(() => { window.dispatchEvent(new Event('settings:tools-updated')); });
    await waitFor(() => expect(useSettingsSelectionStore.getState().allToolNames).toEqual([EXISTING_TOOL.name]));

    await act(async () => { stale.resolve([STALE_TOOL]); });

    expect(useSettingsSelectionStore.getState().allToolNames).toEqual([EXISTING_TOOL.name]);
  });
});
