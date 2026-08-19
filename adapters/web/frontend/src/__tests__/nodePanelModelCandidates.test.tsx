// 节点结构化编辑面板的模型字段。它写的是节点文件的 model，和控制台人格页同一个键，
// 所以候选得包含别的节点那些 $ENV{...} —— 节点这条路会展开它们。
import { fireEvent, render, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getNodeRaw, getNodes, getProviders } from '../api/supervisorClient';
import { AgentsSettingsRightPanel } from '../components/settings/panels/SettingsContextPanels';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getNodeRaw: vi.fn(),
  getNodes: vi.fn(),
  getProviders: vi.fn(),
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

const boxLabelled = (container: HTMLElement, label: string) =>
  [...container.querySelectorAll('input')].find(
    (input) => input.previousElementSibling?.textContent === label,
  ) as HTMLInputElement;

const modelBox = (container: HTMLElement) => boxLabelled(container, '模型');

// 面板里不止一张候选表，按输入框的 list 指向取。
const optionsOf = (container: HTMLElement, input: HTMLInputElement) => {
  const id = input.getAttribute('list');
  if (!id) return [];
  return [...container.querySelectorAll(`#${CSS.escape(id)} option`)].map((node) => (node as HTMLOptionElement).value);
};

const options = (container: HTMLElement) => optionsOf(container, modelBox(container));

beforeEach(() => {
  vi.mocked(getNodeRaw).mockResolvedValue(NODE_YAML);
  vi.mocked(getNodes).mockResolvedValue(NODES as any);
  vi.mocked(getProviders).mockResolvedValue(PROVIDERS as any);
});

describe('节点面板的模型候选', () => {
  it('把渠道模型和别的节点的 $ENV{...} 都摆出来', async () => {
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    expect(options(container)).toContain('gpt-4o-mini');
    expect(options(container)).toContain('$ENV{DRAW_MODEL}');
    // 光渲染出 datalist 不够，输入框得真的指着它。
    expect(modelBox(container).getAttribute('list')).toBe(container.querySelector('datalist')!.id);
  });

  it('不把本节点自己那份摆回它自己的候选里', async () => {
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    expect(options(container)).not.toContain('$ENV{QQ_MAIN_MODEL}');
  });

  it('两个来源都拉不到就退成纯输入框', async () => {
    vi.mocked(getNodes).mockRejectedValue(new Error('403'));
    vi.mocked(getProviders).mockRejectedValue(new Error('403'));
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(modelBox(container)).toBeTruthy());

    expect(container.querySelector('datalist')).toBeNull();
    expect(modelBox(container)).not.toHaveAttribute('list');
    expect(boxLabelled(container, '供应商，可选')).not.toHaveAttribute('list');
  });

  it('供应商框摆的是后端注册过的渠道名', async () => {
    // 节点的 provider 写错了要到运行时才报错，候选表是唯一的事前提示。
    vi.mocked(getProviders).mockResolvedValue({ ...PROVIDERS, registered: ['openai', 'deepseek'] } as any);
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(container.querySelectorAll('datalist')).toHaveLength(2));

    expect(optionsOf(container, boxLabelled(container, '供应商，可选'))).toEqual(['openai', 'deepseek']);
  });

  it('渠道拉不到但节点拉得到时，只有模型有候选', async () => {
    vi.mocked(getProviders).mockRejectedValue(new Error('403'));
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    expect(options(container)).toContain('$ENV{DRAW_MODEL}');
    expect(boxLabelled(container, '供应商，可选')).not.toHaveAttribute('list');
  });

  it('收下不在候选里的值', async () => {
    selectNode();
    const { container } = render(<AgentsSettingsRightPanel />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());
    fireEvent.change(modelBox(container), { target: { value: 'relay/qwen3-max' } });

    expect(modelBox(container)).toHaveValue('relay/qwen3-max');
  });
});
