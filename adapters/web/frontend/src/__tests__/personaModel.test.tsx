// 人格页的模型字段。model 是上游 API 的字面量，前端无从校验 ——
// 候选只能当提示，输入框必须继续收任意值，否则节点里在用的 $ENV{...} 会被界面挡死。
// 连带覆盖 isNotFound：只有以 404 打头的错误才算「文件还没建」。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAllToolNames,
  getNodeFileRaw,
  getNodeFiles,
  getNodeRaw,
  getNodes,
  getProviders,
  isNotFound,
  updateNodeFileRaw,
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
  updateNodeFileRaw: vi.fn(),
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
  source: 'custom',
  same_as_default: false,
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
  vi.mocked(getNodeFileRaw).mockResolvedValue({ content: '人设正文', hash: 'persona-1' });
  vi.mocked(getNodeRaw).mockResolvedValue({ content: ORCHESTRATOR_YAML, hash: 'node-1' });
  vi.mocked(getAllToolNames).mockResolvedValue(['read_file']);
  vi.mocked(getNodes).mockResolvedValue(NODES as any);
  vi.mocked(getProviders).mockResolvedValue(PROVIDERS as any);
  vi.mocked(updateNodeRaw).mockResolvedValue({ ok: true, hash: 'node-2' });
  vi.mocked(updateNodeFileRaw).mockResolvedValue({ ok: true, hash: 'persona-2' });
});

describe('认出「不存在」', () => {
  it('以 404 打头才算', () => {
    expect(isNotFound(new Error('404'))).toBe(true);
    expect(isNotFound(new Error('404 Not Found'))).toBe(true);
    expect(isNotFound(new Error('404 节点文件不存在'))).toBe(true);
  });

  it('数字还没断开就不算，免得 4040 被当成 404', () => {
    expect(isNotFound(new Error('4040 网关把状态码拼错了'))).toBe(false);
    expect(isNotFound(new Error('404500'))).toBe(false);
  });

  it('404 出现在别处不算', () => {
    expect(isNotFound(new Error('Upload failed: 404'))).toBe(false);
    expect(isNotFound(new Error('500 上游返回 404'))).toBe(false);
  });

  it('不是 Error 的一律不算', () => {
    expect(isNotFound('404 Not Found')).toBe(false);
    expect(isNotFound({ message: '404 Not Found' })).toBe(false);
    expect(isNotFound(null)).toBe(false);
    expect(isNotFound(undefined)).toBe(false);
  });
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

  it('状态码只是开头几位碰上 404，照样按读失败算', async () => {
    vi.mocked(getNodeFileRaw).mockRejectedValue(new Error('4040 网关把状态码拼错了'));
    render(<PersonaPage />);

    expect(await screen.findByText('4040 网关把状态码拼错了')).toBeInTheDocument();
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

describe('人设正文的保存基准', () => {
  const editor = () => screen.getByRole('textbox', { name: '人设内容' });
  // 一页两块各有一条底栏，「丢弃」得挑人设那一条。
  const discard = () => within(screen.getByRole('button', { name: '保存人设' }).closest('div')!)
    .getByRole('button', { name: '丢弃' });
  const ready = async () => { render(<PersonaPage />); await waitFor(() => expect(editor()).toHaveValue('人设正文')); };

  it('保存交回读到的那一版指纹，存完换成结果里的新指纹', async () => {
    await ready();

    fireEvent.change(editor(), { target: { value: '改了一版' } });
    fireEvent.click(screen.getByRole('button', { name: '保存人设' }));
    await waitFor(() => expect(updateNodeFileRaw).toHaveBeenCalledOnce());
    fireEvent.change(editor(), { target: { value: '又改一版' } });
    fireEvent.click(screen.getByRole('button', { name: '保存人设' }));
    await waitFor(() => expect(updateNodeFileRaw).toHaveBeenCalledTimes(2));

    expect(vi.mocked(updateNodeFileRaw).mock.calls.map((call) => call[3])).toEqual(['persona-1', 'persona-2']);
  });

  it('文件还没建时指纹是空串 —— 第一次保存要求它仍然不存在', async () => {
    vi.mocked(getNodeFiles).mockResolvedValue([nodeFile('_persona.md', true)]);
    render(<PersonaPage />);
    await screen.findByText(isOnlyExample);

    fireEvent.change(editor(), { target: { value: '头一份人设' } });
    fireEvent.click(screen.getByRole('button', { name: '保存人设' }));

    await waitFor(() => expect(updateNodeFileRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(updateNodeFileRaw).mock.calls[0]).toEqual(['admin-token', '_persona.md', '头一份人设', '']);
  });

  it('指纹对不上时说清楚没保存，草稿留在手里', async () => {
    vi.mocked(updateNodeFileRaw).mockRejectedValue(new Error('409 _persona.md 已被别处改过，刷新后再改'));
    await ready();

    fireEvent.change(editor(), { target: { value: '改了一版' } });
    fireEvent.click(screen.getByRole('button', { name: '保存人设' }));

    expect(await screen.findByText('人设在打开之后被别处改过，没有保存：点「丢弃」读入最新内容，再把改动重做一遍。'))
      .toBeInTheDocument();
    expect(editor()).toHaveValue('改了一版');
  });

  it('别的失败照实说，不冒充成冲突', async () => {
    vi.mocked(updateNodeFileRaw).mockRejectedValue(new Error('500 磁盘满了'));
    await ready();

    fireEvent.change(editor(), { target: { value: '改了一版' } });
    fireEvent.click(screen.getByRole('button', { name: '保存人设' }));

    expect(await screen.findByText('500 磁盘满了')).toBeInTheDocument();
  });

  it('「丢弃」重读磁盘上现在那一份，而不是退回打开时的旧正文', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await ready();
    fireEvent.change(editor(), { target: { value: '改了一版' } });
    vi.mocked(getNodeFileRaw).mockResolvedValue({ content: '别处刚写的人设', hash: 'persona-9' });

    fireEvent.click(discard());

    await waitFor(() => expect(editor()).toHaveValue('别处刚写的人设'));
    expect(getNodeFileRaw).toHaveBeenCalledTimes(2);
  });

  it('「丢弃」被拒绝就不重读，草稿留着', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    await ready();
    fireEvent.change(editor(), { target: { value: '改了一版' } });

    fireEvent.click(discard());

    expect(editor()).toHaveValue('改了一版');
    expect(getNodeFileRaw).toHaveBeenCalledOnce();
  });
});

describe('模型那一块的合并保存', () => {
  it('保存前现读节点文件，别处刚加的键不会被这边的旧底子盖掉', async () => {
    const LATER = `${ORCHESTRATOR_YAML}max_steps: 12\n`;
    vi.mocked(getNodeRaw)
      .mockResolvedValueOnce({ content: ORCHESTRATOR_YAML, hash: 'node-1' })
      .mockResolvedValue({ content: LATER, hash: 'node-9' });
    render(<PersonaPage />);
    await waitFor(() => expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}'));

    fireEvent.change(modelBox(), { target: { value: 'gpt-4o' } });
    fireEvent.click(screen.getByRole('button', { name: '保存模型' }));

    await waitFor(() => expect(updateNodeRaw).toHaveBeenCalledOnce());
    const [, , written, base] = vi.mocked(updateNodeRaw).mock.calls[0];
    expect(written).toBe(LATER.replace('model: $ENV{QQ_MAIN_MODEL}', 'model: gpt-4o'));
    expect(base).toBe('node-9');
  });
});
