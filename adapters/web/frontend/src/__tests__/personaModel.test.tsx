// 人格页的模型字段。model 是上游 API 的字面量，前端无从校验 ——
// 候选只能当提示，输入框必须继续收任意值，否则节点里在用的 $ENV{...} 会被界面挡死。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAllToolNames,
  getNodeFileRaw,
  getNodeFiles,
  getNodeRaw,
  getNodes,
  getProviders,
  updateNodeRaw,
  type NodeFileInfo,
} from '../api/supervisorClient';
import { PersonaPage } from '../console/PersonaPage';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getAllToolNames: vi.fn(),
  getNodeFileRaw: vi.fn(),
  getNodeFiles: vi.fn(),
  getNodeRaw: vi.fn(),
  getNodes: vi.fn(),
  getProviders: vi.fn(),
  updateNodeRaw: vi.fn(),
}));

const ORCHESTRATOR_YAML = [
  'id: qq.orchestrator',
  'type: ai',
  'model: $ENV{QQ_MAIN_MODEL}',
  'tool_access:',
  '  allow:',
  '    - read_file',
  'delegate_targets:',
  '  - qq.vision',
  '',
].join('\n');

const PROVIDERS = {
  active_provider: 'openai',
  providers: {
    openai: { base_url: '', model: 'gpt-4o-mini', api_key_present: true, api_key_redacted: '' },
    deepseek: { base_url: '', model: 'deepseek-v4-pro', api_key_present: true, api_key_redacted: '' },
  },
  fallbacks: [{ provider: 'openai', model: 'claude-sonnet-4-6' }],
  registered: ['openai'],
};

const NODES = [
  { id: 'qq.orchestrator', type: 'ai', model: '$ENV{QQ_MAIN_MODEL}' },
  { id: 'qq.vision', type: 'ai', model: '$ENV{QQ_VISION_MODEL}' },
  { id: 'system.compactor', type: 'ai', model: '$ENV{CLONOTH_COMPACT_MODEL}' },
];

const nodeFile = (name: string, isExample = false): NodeFileInfo => ({
  name,
  path: `config/nodes/${name}`,
  kind: 'fragment',
  suffix: '.md',
  is_example: isExample,
  base_name: name.replace(/\.md$/, ''),
  size: 1,
  updated_at: 0,
});

const ONLY_EXAMPLE = '还没有 _persona.md，bot 现在读的是 _persona.example.md。 在这里保存一次就会创建它，示例从此不再生效。';

// 提示被两个 <code> 切成几段，逐字比对只能落在整个 <p> 的 textContent 上。
const isOnlyExample = (_: string, node: Element | null) => node?.tagName === 'P' && node.textContent === ONLY_EXAMPLE;

const modelBox = () => screen.getByPlaceholderText('留空 = 跟随全局默认模型') as HTMLInputElement;

const optionValues = (container: HTMLElement) =>
  [...container.querySelectorAll('datalist option')].map((node) => (node as HTMLOptionElement).value);

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token' });
  vi.mocked(getNodeFiles).mockResolvedValue([nodeFile('_persona.md')]);
  vi.mocked(getNodeFileRaw).mockResolvedValue('人设正文');
  vi.mocked(getNodeRaw).mockResolvedValue(ORCHESTRATOR_YAML);
  vi.mocked(getAllToolNames).mockResolvedValue(['read_file']);
  vi.mocked(getNodes).mockResolvedValue(NODES as any);
  vi.mocked(getProviders).mockResolvedValue(PROVIDERS as any);
  vi.mocked(updateNodeRaw).mockResolvedValue({ ok: true });
});

describe('人设文件', () => {
  it('文件在就把正文读进编辑框', async () => {
    render(<PersonaPage />);

    await waitFor(() => expect(screen.getByRole('textbox', { name: '人设内容' })).toHaveValue('人设正文'));
    expect(getNodeFileRaw).toHaveBeenCalledWith('admin-token', '_persona.md');
    expect(screen.queryByText(isOnlyExample)).toBeNull();
  });

  // 直接去读不存在的文件，浏览器控制台会留一条谁也修不了的 404 报错。
  it('列表里没有就直接进「只有示例」，raw 接口一次都不碰', async () => {
    vi.mocked(getNodeFiles).mockResolvedValue([nodeFile('_persona.md', true), nodeFile('_tone.md')]);
    render(<PersonaPage />);

    expect(await screen.findByText(isOnlyExample)).toBeInTheDocument();
    expect(getNodeFileRaw).not.toHaveBeenCalled();
    expect(screen.getByRole('textbox', { name: '人设内容' })).toHaveValue('');
  });

  it('查到了读的时候却 404，一样按还没建算，不报错', async () => {
    vi.mocked(getNodeFileRaw).mockRejectedValue(new Error('404 Not Found'));
    render(<PersonaPage />);

    expect(await screen.findByText(isOnlyExample)).toBeInTheDocument();
    expect(screen.queryByText('404 Not Found')).toBeNull();
  });

  it('其它错误照实说，不冒充成还没建', async () => {
    vi.mocked(getNodeFiles).mockRejectedValue(new Error('500 读不到节点文件清单'));
    render(<PersonaPage />);

    expect(await screen.findByText('500 读不到节点文件清单')).toBeInTheDocument();
    expect(screen.queryByText(isOnlyExample)).toBeNull();
    expect(screen.getByRole('textbox', { name: '人设内容' })).toBeDisabled();
  });
});

describe('模型候选', () => {
  it('把渠道、备用链和其它节点已经在用的值摆出来', async () => {
    const { container } = render(<PersonaPage />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    // 全局默认排最前；本节点自己那份不算候选。
    expect(optionValues(container)).toEqual([
      'gpt-4o-mini',
      'deepseek-v4-pro',
      'claude-sonnet-4-6',
      '$ENV{QQ_VISION_MODEL}',
      '$ENV{CLONOTH_COMPACT_MODEL}',
    ]);
    expect(modelBox().getAttribute('list')).toBe(container.querySelector('datalist')!.id);
  });

  it('一头拉不到就只用另一头的', async () => {
    vi.mocked(getProviders).mockRejectedValue(new Error('403'));
    const { container } = render(<PersonaPage />);

    await waitFor(() => expect(container.querySelector('datalist')).not.toBeNull());

    expect(optionValues(container)).toEqual(['$ENV{QQ_VISION_MODEL}', '$ENV{CLONOTH_COMPACT_MODEL}']);
  });

  it('两头都拉不到就退成纯输入框，这一页其余部分照常', async () => {
    vi.mocked(getProviders).mockRejectedValue(new Error('403'));
    vi.mocked(getNodes).mockRejectedValue(new Error('500'));
    const { container } = render(<PersonaPage />);

    await waitFor(() => expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}'));

    expect(container.querySelector('datalist')).toBeNull();
    expect(modelBox()).not.toHaveAttribute('list');
    expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}');
    expect(screen.getByText('人设')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '人设内容' })).toBeInTheDocument();
  });
});

describe('模型输入', () => {
  it('收下不在候选里的值，并原样写进节点文件', async () => {
    render(<PersonaPage />);
    await waitFor(() => expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}'));

    fireEvent.change(modelBox(), { target: { value: '$ENV{MY_RELAY_MODEL}' } });
    expect(modelBox()).toHaveValue('$ENV{MY_RELAY_MODEL}');

    // 两块各自保存，按钮名字不同，点的必须是模型这一块的。
    fireEvent.click(screen.getByRole('button', { name: '保存模型' }));

    await waitFor(() => expect(updateNodeRaw).toHaveBeenCalled());
    expect(vi.mocked(updateNodeRaw).mock.calls[0][2]).toContain('model: $ENV{MY_RELAY_MODEL}');
  });

  it('名字里带 # 就加引号，不然写回去那半截会被读成注释', async () => {
    render(<PersonaPage />);
    await waitFor(() => expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}'));

    fireEvent.change(modelBox(), { target: { value: 'a # b' } });
    fireEvent.click(screen.getByRole('button', { name: '保存模型' }));

    await waitFor(() => expect(updateNodeRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(updateNodeRaw).mock.calls[0][2]).toBe(
      ORCHESTRATOR_YAML.replace('model: $ENV{QQ_MAIN_MODEL}', 'model: "a # b"'),
    );
  });

  it('寻常名字不加引号，免得每存一次都多一行 diff', async () => {
    render(<PersonaPage />);
    await waitFor(() => expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}'));

    fireEvent.change(modelBox(), { target: { value: 'gpt-4o' } });
    fireEvent.click(screen.getByRole('button', { name: '保存模型' }));

    await waitFor(() => expect(updateNodeRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(updateNodeRaw).mock.calls[0][2]).toBe(
      ORCHESTRATOR_YAML.replace('model: $ENV{QQ_MAIN_MODEL}', 'model: gpt-4o'),
    );
  });

  it('清空就把这一行删掉，回落到全局默认模型', async () => {
    render(<PersonaPage />);
    await waitFor(() => expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}'));

    fireEvent.change(modelBox(), { target: { value: '  ' } });
    fireEvent.click(screen.getByRole('button', { name: '保存模型' }));

    await waitFor(() => expect(updateNodeRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(updateNodeRaw).mock.calls[0][2]).toBe(
      ORCHESTRATOR_YAML.replace('model: $ENV{QQ_MAIN_MODEL}\n', ''),
    );
  });

  it('说清楚留空和环境变量两种写法', async () => {
    render(<PersonaPage />);
    await waitFor(() => expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}'));

    expect(modelBox()).toHaveAttribute('placeholder', '留空 = 跟随全局默认模型');
    expect(screen.getByText('$ENV{VAR}')).toBeInTheDocument();
  });
});
