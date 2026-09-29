// 右栏编辑器的「刷新」和按项冲突。MCP 与定时任务是整份文件里的一项：保存时先现读再合并，
// 这一项在打开之后被别处改过就不能拿旧表单盖掉；别的项改了照常合并。
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import yaml from 'js-yaml';

import * as api from '../api/supervisorClient';
import {
  AgentsSettingsRightPanel,
  AutomationSettingsRightPanel,
  McpSettingsRightPanel,
  SkillsSettingsRightPanel,
  ToolsSettingsRightPanel,
} from '../components/settings/panels/SettingsContextPanels';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...await importOriginal<typeof import('../api/supervisorClient')>(),
  getNodes: vi.fn(), getProviders: vi.fn(), getAllToolNames: vi.fn(),
  getNodeRaw: vi.fn(), updateNodeRaw: vi.fn(),
  getToolRaw: vi.fn(), updateToolRaw: vi.fn(), reloadTools: vi.fn(),
  getSkillRaw: vi.fn(), updateSkillRaw: vi.fn(),
  getMcpClientsRaw: vi.fn(), updateMcpClientsRaw: vi.fn(),
  getSchedulesRaw: vi.fn(), updateSchedulesRaw: vi.fn(),
}));

const TOKEN = 'admin-token';

const NODE_YAML = 'id: qq.alpha\ntype: ai\nname: 旧名字\n';
const TOOL_SCRIPT = 'SPEC = {}\n';
const SKILL_MD = '---\nname: alpha\nstrategy: normal\nkeywords: []\n---\n正文\n';

const mcpRaw = (description: string, extra: Record<string, unknown> = {}) => yaml.dump({
  clients: {
    alpha: { transport: 'stdio', command: 'run-me', description, ...extra },
    beta: { transport: 'stdio', command: 'other' },
  },
});

const scheduleRaw = (text: string, extra: Record<string, unknown> = {}) => yaml.dump({
  schedules: [
    { id: 'alpha', cron: '0 * * * *', type: 'message', text, ...extra },
    { id: 'beta', cron: '1 * * * *', type: 'message', text: '别人那条' },
  ],
});

const rawText = (content: string, hash: string): api.RawText => ({ content, hash });

beforeEach(() => {
  vi.resetAllMocks();
  useSettingsStore.setState({ adminToken: TOKEN, isAuthenticated: true });
  useSettingsSelectionStore.setState({
    selectedNode: null, selectedTool: null, selectedSkill: null, selectedScheduleId: null, selectedMcpClient: null,
  });
  vi.mocked(api.getNodes).mockResolvedValue([]);
  vi.mocked(api.getAllToolNames).mockResolvedValue([]);
  vi.mocked(api.getProviders).mockResolvedValue({
    registered: [], providers: {}, active_provider: '', fallbacks: [], node_fallbacks: {}, reserved_names: [],
  } as any);
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const refresh = () => fireEvent.click(screen.getByRole('button', { name: '刷新' }));

describe('节点编辑器', () => {
  const open = async () => {
    useSettingsSelectionStore.setState({ selectedNode: { id: 'qq.alpha', type: 'ai' } as any });
    render(<AgentsSettingsRightPanel />);
    await waitFor(() => expect(screen.getByLabelText('名称')).toHaveValue('旧名字'));
  };

  beforeEach(() => {
    vi.mocked(api.getNodeRaw).mockResolvedValue(rawText(NODE_YAML, 'node-1'));
    vi.mocked(api.updateNodeRaw).mockResolvedValue({ ok: true, hash: 'node-2' });
  });

  it('保存交回加载时那一版指纹，存完换成结果里的新指纹', async () => {
    await open();

    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '新名字' } });
    fireEvent.click(screen.getByRole('button', { name: '保存节点配置' }));
    await waitFor(() => expect(api.updateNodeRaw).toHaveBeenCalledOnce());
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '再改一次' } });
    fireEvent.click(screen.getByRole('button', { name: '保存节点配置' }));
    await waitFor(() => expect(api.updateNodeRaw).toHaveBeenCalledTimes(2));

    expect(vi.mocked(api.updateNodeRaw).mock.calls.map(call => call[3])).toEqual(['node-1', 'node-2']);
  });

  it('「刷新」重读文件，换上别处刚写下的那一版', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await open();
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '本地草稿' } });
    vi.mocked(api.getNodeRaw).mockResolvedValue(rawText('id: qq.alpha\ntype: ai\nname: 别处改的\n', 'node-9'));

    refresh();

    await waitFor(() => expect(screen.getByLabelText('名称')).toHaveValue('别处改的'));
    expect(api.getNodeRaw).toHaveBeenCalledTimes(2);
  });

  it('有草稿时「刷新」先问一声，拒绝就不重读', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await open();
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '本地草稿' } });

    refresh();

    expect(confirm).toHaveBeenCalledOnce();
    expect(api.getNodeRaw).toHaveBeenCalledOnce();
    expect(screen.getByLabelText('名称')).toHaveValue('本地草稿');
  });

  it('没有草稿时「刷新」不问，直接重读', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    await open();

    refresh();

    await waitFor(() => expect(api.getNodeRaw).toHaveBeenCalledTimes(2));
    expect(confirm).not.toHaveBeenCalled();
  });

  it('读不出来的节点也留着「刷新」，不然这一栏只能换个节点再换回来', async () => {
    vi.mocked(api.getNodeRaw).mockRejectedValueOnce(new Error('500 读不到节点文件'));
    useSettingsSelectionStore.setState({ selectedNode: { id: 'qq.alpha', type: 'ai' } as any });
    render(<AgentsSettingsRightPanel />);
    await screen.findByText('500 读不到节点文件');

    refresh();

    await waitFor(() => expect(screen.getByLabelText('名称')).toHaveValue('旧名字'));
  });
});

describe('工具与技能编辑器', () => {
  it('工具脚本保存交回加载时的指纹，存完换新', async () => {
    vi.mocked(api.getToolRaw).mockResolvedValue(rawText(TOOL_SCRIPT, 'tool-1'));
    vi.mocked(api.updateToolRaw).mockResolvedValue({ ok: true, hash: 'tool-2' });
    useSettingsSelectionStore.setState({
      selectedTool: { name: 'demo', source: 'external', editable: true, description: '', input_schema: {}, timeout_sec: 30, has_spec: true } as any,
    });
    render(<ToolsSettingsRightPanel />);
    const editor = await screen.findByLabelText('工具脚本');
    await waitFor(() => expect(editor).toHaveValue(TOOL_SCRIPT));

    fireEvent.change(editor, { target: { value: 'SPEC = {"a": 1}\n' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(api.updateToolRaw).toHaveBeenCalledOnce());
    fireEvent.change(editor, { target: { value: 'SPEC = {"a": 2}\n' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(api.updateToolRaw).toHaveBeenCalledTimes(2));

    expect(vi.mocked(api.updateToolRaw).mock.calls.map(call => call[3])).toEqual(['tool-1', 'tool-2']);
  });

  it('工具的「刷新」重读脚本', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.getToolRaw).mockResolvedValue(rawText(TOOL_SCRIPT, 'tool-1'));
    useSettingsSelectionStore.setState({
      selectedTool: { name: 'demo', source: 'external', editable: true, description: '', input_schema: {}, timeout_sec: 30, has_spec: true } as any,
    });
    render(<ToolsSettingsRightPanel />);
    const editor = await screen.findByLabelText('工具脚本');
    await waitFor(() => expect(editor).toHaveValue(TOOL_SCRIPT));
    vi.mocked(api.getToolRaw).mockResolvedValue(rawText('SPEC = {"from": "elsewhere"}\n', 'tool-9'));

    refresh();

    await waitFor(() => expect(screen.getByLabelText('工具脚本')).toHaveValue('SPEC = {"from": "elsewhere"}\n'));
  });

  it('技能的快捷字段和原文用同一份底子，存完一起换新', async () => {
    vi.mocked(api.getSkillRaw).mockResolvedValue(rawText(SKILL_MD, 'skill-1'));
    vi.mocked(api.updateSkillRaw).mockResolvedValue({ ok: true, hash: 'skill-2' });
    useSettingsSelectionStore.setState({
      selectedSkill: { name: 'alpha', enabled: true, strategy: 'normal', keywords: [] } as any,
    });
    render(<SkillsSettingsRightPanel />);
    await waitFor(() => expect(screen.getByLabelText('策略')).toHaveValue('normal'));

    fireEvent.change(screen.getByLabelText('order'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: '保存快捷字段' }));
    await waitFor(() => expect(api.updateSkillRaw).toHaveBeenCalledOnce());
    fireEvent.change(screen.getByLabelText('order'), { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: '保存快捷字段' }));
    await waitFor(() => expect(api.updateSkillRaw).toHaveBeenCalledTimes(2));

    expect(vi.mocked(api.updateSkillRaw).mock.calls.map(call => call[3])).toEqual(['skill-1', 'skill-2']);
  });

  it('技能的「刷新」重读 Markdown', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.getSkillRaw).mockResolvedValue(rawText(SKILL_MD, 'skill-1'));
    useSettingsSelectionStore.setState({
      selectedSkill: { name: 'alpha', enabled: true, strategy: 'normal', keywords: [] } as any,
    });
    render(<SkillsSettingsRightPanel />);
    await waitFor(() => expect(screen.getByLabelText('策略')).toHaveValue('normal'));
    vi.mocked(api.getSkillRaw).mockResolvedValue(rawText(SKILL_MD.replace('normal', 'constant'), 'skill-9'));

    refresh();

    await waitFor(() => expect(screen.getByLabelText('策略')).toHaveValue('constant'));
  });
});

describe('MCP Client 按项保存', () => {
  const STALE = '这个 Client 打开之后被别处改过，点「刷新」读入最新内容再改。';

  const open = async (raw = mcpRaw('原来的说明')) => {
    vi.mocked(api.getMcpClientsRaw).mockResolvedValue(rawText(raw, 'mcp-1'));
    vi.mocked(api.updateMcpClientsRaw).mockResolvedValue({ ok: true, hash: 'mcp-2' });
    useSettingsSelectionStore.setState({ selectedMcpClient: { id: 'alpha', transport: 'stdio', enabled: true } });
    render(<McpSettingsRightPanel />);
    await waitFor(() => expect(screen.getByLabelText('description')).toHaveValue('原来的说明'));
  };

  const edit = (value: string) => fireEvent.change(screen.getByLabelText('description'), { target: { value } });
  const save = () => fireEvent.click(screen.getByRole('button', { name: '保存' }));

  it('这一项在打开之后被别处改过就不写，说清楚怎么办', async () => {
    await open();
    edit('我这边改的');
    vi.mocked(api.getMcpClientsRaw).mockResolvedValue(rawText(mcpRaw('别处改的'), 'mcp-9'));

    save();

    expect(await screen.findByText(STALE)).toBeInTheDocument();
    expect(api.updateMcpClientsRaw).not.toHaveBeenCalled();
    expect(screen.getByLabelText('description')).toHaveValue('我这边改的');
  });

  it('别处改的是另一项就照常合并，带的是刚读那一版的指纹', async () => {
    await open();
    edit('我这边改的');
    const latest = yaml.dump({
      clients: {
        alpha: { transport: 'stdio', command: 'run-me', description: '原来的说明' },
        beta: { transport: 'stdio', command: '别处换的命令' },
      },
    });
    vi.mocked(api.getMcpClientsRaw).mockResolvedValue(rawText(latest, 'mcp-9'));

    save();

    await waitFor(() => expect(api.updateMcpClientsRaw).toHaveBeenCalledOnce());
    const [, content, base] = vi.mocked(api.updateMcpClientsRaw).mock.calls[0];
    expect(yaml.load(content)).toMatchObject({
      clients: { alpha: { description: '我这边改的' }, beta: { command: '别处换的命令' } },
    });
    expect(base).toBe('mcp-9');
  });

  it('不重载页面连存两次也不误报冲突 —— 存完要拿刚写下的那一版当底子', async () => {
    await open();
    let onDisk = mcpRaw('原来的说明');
    vi.mocked(api.getMcpClientsRaw).mockImplementation(async () => rawText(onDisk, 'mcp-x'));
    vi.mocked(api.updateMcpClientsRaw).mockImplementation(async (_token, content) => {
      onDisk = content;
      return { ok: true, hash: 'mcp-y' };
    });

    edit('第一次');
    save();
    await waitFor(() => expect(api.updateMcpClientsRaw).toHaveBeenCalledOnce());
    edit('第二次');
    save();
    await waitFor(() => expect(api.updateMcpClientsRaw).toHaveBeenCalledTimes(2));

    expect(screen.queryByText(STALE)).toBeNull();
    expect(yaml.load(onDisk)).toMatchObject({ clients: { alpha: { description: '第二次' } } });
  });

  it('「刷新」重读整份文件，换上别处改过的这一项', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await open();
    edit('我这边改的');
    vi.mocked(api.getMcpClientsRaw).mockResolvedValue(rawText(mcpRaw('别处改的'), 'mcp-9'));

    refresh();

    await waitFor(() => expect(screen.getByLabelText('description')).toHaveValue('别处改的'));
  });

  it('找不到这一项时也留着「刷新」', async () => {
    vi.mocked(api.getMcpClientsRaw).mockResolvedValue(rawText(yaml.dump({ clients: {} }), 'mcp-1'));
    useSettingsSelectionStore.setState({ selectedMcpClient: { id: 'alpha', transport: 'stdio', enabled: true } });
    render(<McpSettingsRightPanel />);
    await screen.findByText('未在 MCP YAML 中找到此 Client。');

    vi.mocked(api.getMcpClientsRaw).mockResolvedValue(rawText(mcpRaw('回来了'), 'mcp-9'));
    refresh();

    await waitFor(() => expect(screen.getByLabelText('description')).toHaveValue('回来了'));
  });
});

describe('定时任务按项保存', () => {
  const STALE = '这个定时任务打开之后被别处改过，点「刷新」读入最新内容再改。';

  const open = async (raw = scheduleRaw('原来的正文')) => {
    vi.mocked(api.getSchedulesRaw).mockResolvedValue(rawText(raw, 'sched-1'));
    vi.mocked(api.updateSchedulesRaw).mockResolvedValue({ ok: true, hash: 'sched-2' });
    useSettingsSelectionStore.setState({ selectedScheduleId: 'alpha' });
    render(<AutomationSettingsRightPanel />);
    await waitFor(() => expect(screen.getByLabelText('text')).toHaveValue('原来的正文'));
  };

  const edit = (value: string) => fireEvent.change(screen.getByLabelText('text'), { target: { value } });
  const save = () => fireEvent.click(screen.getByRole('button', { name: '保存' }));

  it('这一条在打开之后被别处改过就不写', async () => {
    await open();
    edit('我这边改的');
    vi.mocked(api.getSchedulesRaw).mockResolvedValue(rawText(scheduleRaw('别处改的'), 'sched-9'));

    save();

    expect(await screen.findByText(STALE)).toBeInTheDocument();
    expect(api.updateSchedulesRaw).not.toHaveBeenCalled();
  });

  it('别处改的是另一条就照常合并', async () => {
    await open();
    edit('我这边改的');
    const latest = yaml.dump({
      schedules: [
        { id: 'alpha', cron: '0 * * * *', type: 'message', text: '原来的正文' },
        { id: 'beta', cron: '30 * * * *', type: 'message', text: '别人那条' },
      ],
    });
    vi.mocked(api.getSchedulesRaw).mockResolvedValue(rawText(latest, 'sched-9'));

    save();

    await waitFor(() => expect(api.updateSchedulesRaw).toHaveBeenCalledOnce());
    const [, content, base] = vi.mocked(api.updateSchedulesRaw).mock.calls[0];
    expect(yaml.load(content)).toMatchObject({
      schedules: [{ id: 'alpha', text: '我这边改的' }, { id: 'beta', cron: '30 * * * *' }],
    });
    expect(base).toBe('sched-9');
  });

  it('连存两次不误报冲突', async () => {
    await open();
    let onDisk = scheduleRaw('原来的正文');
    vi.mocked(api.getSchedulesRaw).mockImplementation(async () => rawText(onDisk, 'sched-x'));
    vi.mocked(api.updateSchedulesRaw).mockImplementation(async (_token, content) => {
      onDisk = content;
      return { ok: true, hash: 'sched-y' };
    });

    edit('第一次');
    save();
    await waitFor(() => expect(api.updateSchedulesRaw).toHaveBeenCalledOnce());
    edit('第二次');
    save();
    await waitFor(() => expect(api.updateSchedulesRaw).toHaveBeenCalledTimes(2));

    expect(screen.queryByText(STALE)).toBeNull();
    expect((yaml.load(onDisk) as any).schedules[0]).toMatchObject({ id: 'alpha', text: '第二次' });
  });

  it('「刷新」有草稿时先确认，拒绝就不重读', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await open();
    edit('我这边改的');

    refresh();

    expect(confirm).toHaveBeenCalledOnce();
    expect(api.getSchedulesRaw).toHaveBeenCalledOnce();
    expect(screen.getByLabelText('text')).toHaveValue('我这边改的');
  });

  it('确认之后重读，换上磁盘上现在那一条', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await open();
    edit('我这边改的');
    vi.mocked(api.getSchedulesRaw).mockResolvedValue(rawText(scheduleRaw('别处改的'), 'sched-9'));

    await act(async () => { refresh(); });

    expect(screen.getByLabelText('text')).toHaveValue('别处改的');
  });
});
