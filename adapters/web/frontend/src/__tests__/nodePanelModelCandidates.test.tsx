// 节点结构化编辑面板的渠道与模型字段。渠道是封闭下拉（写错要到运行时才报错），
// 模型仍是自由输入 —— 中转的模型名后端不认，但候选里该有能拉到的那些。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getNodeRaw, getNodes, getProviders, listUpstreamModels } from '../api/supervisorClient';
import { AgentsSettingsRightPanel } from '../components/settings/panels/SettingsContextPanels';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getNodeRaw: vi.fn(),
  getNodes: vi.fn(),
  getProviders: vi.fn(),
  listUpstreamModels: vi.fn(),
}));

const PROVIDERS = {
  active_provider: 'openai',
  providers: {
    openai: { base_url: '', model: 'gpt-4o-mini', api_key_present: true, api_key_redacted: '' },
  },
  fallbacks: [],
  registered: ['openai'],
};

const NODES = [
  { id: 'qq.orchestrator', name: 'QQ', type: 'ai', model: '$ENV{QQ_MAIN_MODEL}' },
  { id: 'draw.image_gen', name: 'Draw', type: 'ai', model: '$ENV{DRAW_MODEL}' },
];

const NODE_YAML = 'id: qq.orchestrator\ntype: ai\nname: QQ\n';

const selectNode = (id = 'qq.orchestrator') => {
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  useSettingsSelectionStore.setState({ selectedNode: { id, name: id, type: 'ai' } as any });
};

const modelBox = () => screen.getByLabelText('模型') as HTMLInputElement;
const channelBox = () => screen.getByLabelText('渠道') as HTMLSelectElement;
const fetchButton = () => screen.getByRole('button', { name: /拉取模型/ }) as HTMLButtonElement;

const modelOptions = (container: HTMLElement) => {
  const id = modelBox().getAttribute('list');
  if (!id) return [];
  return [...container.querySelectorAll(`#${CSS.escape(id)} option`)]
    .map((node) => (node as HTMLOptionElement).value);
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getNodeRaw).mockResolvedValue(NODE_YAML);
  vi.mocked(getNodes).mockResolvedValue(NODES as any);
  vi.mocked(getProviders).mockResolvedValue(PROVIDERS as any);
  vi.mocked(listUpstreamModels).mockResolvedValue([]);
});

describe('模型候选', () => {
  it('把渠道模型和别的节点的 $ENV{...} 都摆出来', async () => {
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    expect(modelOptions(container)).toContain('gpt-4o-mini');
    expect(modelOptions(container)).toContain('$ENV{DRAW_MODEL}');
  });

  it('不把本节点自己那份摆回它自己的候选里', async () => {
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    expect(modelOptions(container)).not.toContain('$ENV{QQ_MAIN_MODEL}');
  });

  it('两个来源都拉不到就退成纯输入框', async () => {
    vi.mocked(getNodes).mockRejectedValue(new Error('403'));
    vi.mocked(getProviders).mockRejectedValue(new Error('403'));
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(modelBox()).toBeTruthy());

    expect(container.querySelector('datalist')).toBeNull();
    expect(modelBox()).not.toHaveAttribute('list');
  });

  it('收下不在候选里的值', async () => {
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());
    fireEvent.change(modelBox(), { target: { value: 'relay/qwen3-max' } });

    expect(modelBox()).toHaveValue('relay/qwen3-max');
  });
});

const groupOf = (value: string): string => {
  const option = [...channelBox().options].find((item) => item.value === value);
  return (option?.parentElement as HTMLOptGroupElement | null)?.label || '';
};

describe('渠道下拉', () => {
  it('只列后端认的名字，外加一个跟随全局', async () => {
    // 手打的名字要到运行时才报错，封闭下拉是唯一的事前防线。
    vi.mocked(getProviders).mockResolvedValue({ ...PROVIDERS, registered: ['openai', 'deepseek'] } as any);
    selectNode();
    render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect([...channelBox().options].length).toBe(3));

    expect([...channelBox().options].map((option) => option.value))
      .toEqual(['', 'openai', 'deepseek']);
  });

  it('已配渠道排在裸线格式前面，分两组', async () => {
    vi.mocked(getProviders).mockResolvedValue({
      ...PROVIDERS,
      providers: {
        ...PROVIDERS.providers,
        'gemini-中转A': { base_url: '', model: '', api_key_present: true, type: 'gemini' },
      },
      registered: ['openai', 'gemini'],
    } as any);
    selectNode();
    render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect([...channelBox().options].length).toBe(4));

    expect([...channelBox().options].map((option) => option.value))
      .toEqual(['', 'openai', 'gemini-中转A', 'gemini']);
    expect(groupOf('gemini-中转A')).toBe('已配渠道');
    expect(groupOf('gemini')).toBe('线格式');
  });

  it('同一家可以配好几个，各是各的一项', async () => {
    vi.mocked(getProviders).mockResolvedValue({
      ...PROVIDERS,
      providers: {
        'gemini-中转A': { base_url: '', model: '', api_key_present: true, type: 'gemini', label: '便宜' },
        'gemini-官方': { base_url: '', model: '', api_key_present: true, type: 'gemini' },
      },
      registered: ['gemini'],
    } as any);
    selectNode();
    render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect([...channelBox().options].length).toBe(4));

    const texts = [...channelBox().options].map((option) => option.textContent);
    // 备注跟在名字后面，否则两个 gemini 摆一起分不出谁是谁。
    expect(texts).toContain('gemini-中转A · 便宜');
    expect(texts).toContain('gemini-官方');
  });

  it('yaml 里手写的野名字照样列出来，不会被悄悄改掉', async () => {
    vi.mocked(getNodeRaw).mockResolvedValue(NODE_YAML + 'provider: some-relay\n');
    selectNode();
    render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(channelBox().value).toBe('some-relay'));

    expect([...channelBox().options].map((option) => option.textContent))
      .toContain('some-relay（后端不认）');
  });
});

describe('拉取模型', () => {
  it('没选渠道时按钮是灰的', async () => {
    selectNode();
    render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(channelBox()).toBeTruthy());

    expect(fetchButton().disabled).toBe(true);
  });

  it('选了渠道就能问上游要模型，地址密钥交给后端', async () => {
    vi.mocked(listUpstreamModels).mockResolvedValue(['gemini-3-pro', 'gemini-3-flash']);
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(channelBox()).toBeTruthy());
    fireEvent.change(channelBox(), { target: { value: 'openai' } });
    fireEvent.click(fetchButton());

    await waitFor(() => expect(listUpstreamModels).toHaveBeenCalledWith('admin-token', 'openai', {}));
    await waitFor(() => expect(modelOptions(container)).toContain('gemini-3-pro'));
  });

  it('拉失败就把原因说出来，不清空已有候选', async () => {
    vi.mocked(listUpstreamModels).mockRejectedValue(new Error('上游返回 401'));
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(channelBox()).toBeTruthy());
    fireEvent.change(channelBox(), { target: { value: 'openai' } });
    fireEvent.click(fetchButton());

    await waitFor(() => expect(screen.getByText('上游返回 401')).toBeInTheDocument());
    expect(modelOptions(container)).toContain('gpt-4o-mini');
  });

  it('换渠道会丢掉上一个渠道拉来的清单', async () => {
    vi.mocked(listUpstreamModels).mockResolvedValue(['only-on-openai']);
    vi.mocked(getProviders).mockResolvedValue({ ...PROVIDERS, registered: ['openai', 'deepseek'] } as any);
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(channelBox()).toBeTruthy());
    fireEvent.change(channelBox(), { target: { value: 'openai' } });
    fireEvent.click(fetchButton());
    await waitFor(() => expect(modelOptions(container)).toContain('only-on-openai'));

    fireEvent.change(channelBox(), { target: { value: 'deepseek' } });

    expect(modelOptions(container)).not.toContain('only-on-openai');
  });
});
