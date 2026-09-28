// 技能 / 工具 / 定时任务 / MCP / 节点五个列表页的提示：写操作紧接着就重读列表，
// 刚写下的那句不能被这一次重读冲掉，也不能被右栏保存引起的后台重读冲掉；
// 重读失败时原因接在它后面，下一次读成功只去掉原因，只有点「刷新」两样一起清。
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

const READ_FAILURE = '503 列表读不回来';

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
  /** 右栏保存之后广播的事件名。 */
  event: string;
  refreshLabel: string;
  create: () => void;
  createdText: string;
  /** 造一个选中项，删除按钮才点得动。 */
  select: () => void;
  deleteLabel: string;
  deletedText: string;
}

const PAGES: PageCase[] = [
  {
    title: '技能页',
    Page: SkillsSettingsPage,
    reader: vi.mocked(api.getSkills),
    listValue: [EXISTING_SKILL],
    writeReads: 1,
    event: 'settings:skills-updated',
    refreshLabel: '刷新技能',
    create: () => { fill('创建技能', 'demo_skill'); click('创建技能'); },
    createdText: '技能已创建，请在右栏编辑 frontmatter 和正文。',
    select: () => useSettingsSelectionStore.setState({ selectedSkill: EXISTING_SKILL }),
    deleteLabel: '删除选中技能',
    deletedText: '技能已删除',
  },
  {
    title: '工具页',
    Page: ToolsSettingsPage,
    reader: vi.mocked(api.getTools),
    listValue: [EXISTING_TOOL],
    writeReads: 1,
    event: 'settings:tools-updated',
    refreshLabel: '刷新工具',
    create: () => { fill('创建工具', 'demo_tool'); click('创建工具'); },
    createdText: '工具已创建，请在右栏编辑 Python 脚本。',
    select: () => useSettingsSelectionStore.setState({ selectedTool: EXISTING_TOOL }),
    deleteLabel: '删除选中工具',
    deletedText: '工具已删除',
  },
  {
    title: '定时任务页',
    Page: AutomationSettingsPage,
    reader: vi.mocked(api.getSchedulesRaw),
    listValue: SCHEDULES_RAW,
    writeReads: 2,
    event: 'settings:schedules-updated',
    refreshLabel: '刷新',
    create: () => { fill('创建定时任务', 'demo_schedule'); click('创建任务'); },
    createdText: '任务已创建但尚未启用，请在右栏填写内容后启用。',
    select: () => useSettingsSelectionStore.setState({ selectedScheduleId: 'existing_schedule' }),
    deleteLabel: '删除选中任务',
    deletedText: '定时任务已删除',
  },
  {
    title: 'MCP 页',
    Page: McpSettingsPage,
    reader: vi.mocked(api.getMcpClients),
    listValue: [EXISTING_CLIENT],
    writeReads: 1,
    event: 'settings:mcp-updated',
    refreshLabel: '刷新 MCP',
    create: () => { fill('创建 MCP Client', 'demo_client'); click('创建 Client'); },
    createdText: 'Client 已创建但尚未启用，请在右栏填写连接信息后启用。',
    select: () => useSettingsSelectionStore.setState({ selectedMcpClient: EXISTING_CLIENT }),
    deleteLabel: '删除选中 Client',
    deletedText: 'MCP Client 已删除',
  },
  {
    title: '节点页',
    Page: AgentsSettingsPage,
    reader: vi.mocked(api.getNodes),
    listValue: [EXISTING_NODE],
    writeReads: 1,
    event: 'settings:nodes-updated',
    refreshLabel: '刷新节点',
    create: () => { fill('创建节点', 'demo_node'); click('创建节点'); },
    createdText: '节点已创建，请在右栏继续编辑 YAML。',
    select: () => useSettingsSelectionStore.setState({ selectedNode: EXISTING_NODE }),
    deleteLabel: '删除选中节点',
    deletedText: '节点已删除',
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  useSettingsSelectionStore.setState({
    selectedNode: null, selectedTool: null, selectedSkill: null, selectedScheduleId: null, selectedMcpClient: null,
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

  /** 铺到写操作那次重读为止：前面照常给，最后一次读失败。 */
  const failWriteReload = () => {
    for (let i = 0; i < page.writeReads; i += 1) page.reader.mockResolvedValueOnce(page.listValue);
    page.reader.mockRejectedValueOnce(new Error(READ_FAILURE));
  };

  /** 右栏保存之后广播的那个事件，页面收到就在后台重读一次。 */
  const backgroundReload = async () => {
    act(() => { window.dispatchEvent(new Event(page.event)); });
    await waitFor(() => expect(page.reader).toHaveBeenCalledTimes(2 + page.writeReads));
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

    act(() => { useSettingsStore.setState({ adminToken: 'another-token' }); });

    await waitFor(() => expect(page.reader).toHaveBeenCalledTimes(2 + page.writeReads));
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
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
});
