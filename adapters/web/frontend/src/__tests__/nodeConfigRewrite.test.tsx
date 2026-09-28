// 节点文件的结构化写回：只重写表单改了的键，手写注释和块标量 prompt 原样留着；
// 没保住时面板要在成功提示后面把话说清楚。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import yaml from 'js-yaml';

import * as api from '../api/supervisorClient';
import { AgentsSettingsRightPanel } from '../components/settings/panels/SettingsContextPanels';
import { parseNodeConfig, serializeNodeConfig } from '../components/settings/settingsStructuredConfig';
import { BLOCK_COMMENTS_DROPPED, WHOLE_FILE_REWRITTEN } from '../console/yamlRewrite';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getNodeRaw: vi.fn(),
  getNodes: vi.fn(),
  getProviders: vi.fn(),
  updateNodeRaw: vi.fn(),
}));

const NODE_YAML = [
  '# 综合入口。下面每一段说明都是人手写的。',
  'id: qq.orchestrator',
  'name: 旧名字',
  'description: QQ 主入口',
  'type: ai',
  '# 留空跟随全局默认模型',
  'model: $ENV{QQ_MAIN_MODEL}',
  'persistent: false',
  'prompt: |',
  '  你是一个 QQ 机器人。',
  '  # 这一行是提示词正文，不是注释。',
  '',
  '  说话短一点。',
  'delegate_targets:',
  '  - qq.vision',
  'tool_access:',
  '  allow:',
  '    - read_file',
  '',
].join('\n');

// 顶层写成流式映射，逐键改写插出来的行接不回那一行，只能整份重写 —— 注释就是这么丢的。
const UNREADABLE_YAML = [
  '# 这段注释保不住',
  '{name: 旧名字, id: qq.orchestrator, description: QQ 主入口, type: ai, model: "", persistent: false, delegate_targets: []}',
  '',
].join('\n');

// 一次删掉两个委派目标，列表只能整块重写，夹在条目之间的注释跟着没了。
const LISTED_YAML = [
  '# 这段注释在列表外面，保得住。',
  'id: qq.orchestrator',
  'name: 旧名字',
  'description: QQ 主入口',
  'type: ai',
  'model: ""',
  'persistent: false',
  'delegate_targets:',
  '  - qq.vision',
  '  # 这条只在夜间用',
  '  - qq.search',
  '  - qq.draw',
  '',
].join('\n');

const renderPanel = (raw: string) => {
  vi.mocked(api.getNodeRaw).mockResolvedValue(raw);
  useSettingsSelectionStore.setState({ selectedNode: { id: 'qq.orchestrator', type: 'ai' } });
  return render(<AgentsSettingsRightPanel />);
};

const rename = async (next: string) => {
  const field = await screen.findByLabelText('名称');
  await waitFor(() => expect(field).toHaveValue('旧名字'));
  fireEvent.change(field, { target: { value: next } });
  fireEvent.click(screen.getByRole('button', { name: '保存节点配置' }));
};

const setDelegates = async (next: string) => {
  const field = await screen.findByLabelText('委派目标，使用英文逗号分隔');
  await waitFor(() => expect(field).toHaveValue('qq.vision, qq.search, qq.draw'));
  fireEvent.change(field, { target: { value: next } });
  fireEvent.click(screen.getByRole('button', { name: '保存节点配置' }));
};

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  useSettingsSelectionStore.setState({ selectedNode: null });
  vi.mocked(api.getNodes).mockResolvedValue([]);
  vi.mocked(api.getProviders).mockResolvedValue({
    registered: [], providers: {}, active_provider: '', fallbacks: [], node_fallbacks: {}, reserved_names: [],
  } as any);
  vi.mocked(api.updateNodeRaw).mockResolvedValue({ ok: true });
});

describe('serializeNodeConfig', () => {
  it('只改 name 时注释和块标量 prompt 一个字符都不动', () => {
    const form = parseNodeConfig(NODE_YAML);
    const { content, preserved, note } = serializeNodeConfig(NODE_YAML, { ...form, name: '新名字' });

    expect(preserved).toBe(true);
    expect(note).toBe('');
    expect(content).toBe(NODE_YAML.replace('name: 旧名字', 'name: 新名字'));
    // prompt 正文里那行 # 是提示词的一部分，重写一轮之后不能被当注释吃掉。
    expect((yaml.load(content) as { prompt: string }).prompt).toBe(form.prompt);
  });

  it('原文有认不出的写法时如实报整份重写', () => {
    const form = parseNodeConfig(UNREADABLE_YAML);
    const { content, preserved, note } = serializeNodeConfig(UNREADABLE_YAML, { ...form, name: '新名字' });

    expect(preserved).toBe(false);
    expect(note).toBe(WHOLE_FILE_REWRITTEN);
    expect(content).not.toContain('# 这段注释保不住');
    expect(yaml.load(content)).toMatchObject({ id: 'qq.orchestrator', name: '新名字', type: 'ai' });
  });

  it('只有那一块重写时说的是块里的注释没了，块外的照旧', () => {
    const form = parseNodeConfig(LISTED_YAML);
    const { content, preserved, note } = serializeNodeConfig(LISTED_YAML, { ...form, delegate_targetsText: 'qq.vision' });

    expect(preserved).toBe(false);
    expect(note).toBe(BLOCK_COMMENTS_DROPPED);
    expect(content).toContain('# 这段注释在列表外面，保得住。');
    expect(content).not.toContain('# 这条只在夜间用');
    expect(yaml.load(content)).toMatchObject({ delegate_targets: ['qq.vision'] });
  });
});

describe('节点面板的保存提示', () => {
  it('注释保住了就只说保存成功', async () => {
    renderPanel(NODE_YAML);

    await rename('新名字');

    await waitFor(() => expect(api.updateNodeRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateNodeRaw).mock.calls[0][2]).toBe(NODE_YAML.replace('name: 旧名字', 'name: 新名字'));
    expect(await screen.findByText('节点配置已保存。')).toBeInTheDocument();
  });

  it('没保住就在后面把话说清楚', async () => {
    renderPanel(UNREADABLE_YAML);

    await rename('新名字');

    await waitFor(() => expect(api.updateNodeRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateNodeRaw).mock.calls[0][2]).not.toContain('# 这段注释保不住');
    expect(await screen.findByText(`节点配置已保存。${WHOLE_FILE_REWRITTEN}。`)).toBeInTheDocument();
  });

  it('只丢了块里的注释就说这一句，不冒充成整份重写', async () => {
    renderPanel(LISTED_YAML);

    await setDelegates('qq.vision');

    await waitFor(() => expect(api.updateNodeRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateNodeRaw).mock.calls[0][2]).toContain('# 这段注释在列表外面，保得住。');
    expect(await screen.findByText(`节点配置已保存。${BLOCK_COMMENTS_DROPPED}。`)).toBeInTheDocument();
  });
});

describe('节点面板的来源提示', () => {
  const renderNode = (source?: api.OverlaySource) => {
    vi.mocked(api.getNodeRaw).mockResolvedValue(NODE_YAML);
    useSettingsSelectionStore.setState({ selectedNode: { id: 'qq.orchestrator', type: 'ai', source } });
    render(<AgentsSettingsRightPanel />);
  };

  it('编辑仓库默认那份时先说清楚保存落到哪', async () => {
    renderNode('default');

    expect(await screen.findByText('这是仓库默认节点。保存后会在 config/nodes 下存一份本实例的版本，从此不再随仓库更新。')).toBeInTheDocument();
  });

  it('已经是本实例那份就改说去哪恢复默认', async () => {
    renderNode('override');

    expect(await screen.findByText('本实例改过的版本，存在 config/nodes 下，盖住仓库默认；在节点管理页可以恢复默认。')).toBeInTheDocument();
  });

  it('自建的和老后端不给来源的都不摆这句', async () => {
    renderNode('custom');

    await waitFor(() => expect(screen.getByLabelText('名称')).toHaveValue('旧名字'));
    expect(screen.queryByText(/config\/nodes 下存一份本实例的版本/)).toBeNull();
    expect(screen.queryByText(/在节点管理页可以恢复默认/)).toBeNull();
  });
});
