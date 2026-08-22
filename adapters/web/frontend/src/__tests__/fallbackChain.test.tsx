// 备选链编辑。这一页读到的是脱敏视图，所以最要紧的是「界面看不见的东西不能被存没」：
// 内联密钥、支持图片、手写进 yaml 的参数，都靠提交原始位置由后端合并保住。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  clearNodeFallbacks,
  getNodeRaw,
  getNodes,
  getProviderProfiles,
  getProviders,
  getRuntimeRaw,
  updateFallbacks,
  updateNodeFallbacks,
} from '../api/supervisorClient';
import { ModelsPage } from '../console/ModelsPage';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  clearNodeFallbacks: vi.fn(),
  getNodeRaw: vi.fn(),
  getNodes: vi.fn(),
  getProviderProfiles: vi.fn(),
  getProviders: vi.fn(),
  getRuntimeRaw: vi.fn(),
  updateFallbacks: vi.fn(),
  updateNodeFallbacks: vi.fn(),
}));

const CATALOG = {
  openai: [
    {
      key: 'reasoning_effort',
      label: '思考档位',
      kind: 'enum',
      default: 'default',
      choices: [{ value: 'default', label: '不指定' }, { value: 'high', label: '高' }],
    },
  ],
  deepseek: [{ key: 'thinking', label: '思考模式', kind: 'bool', default: true }],
};

const PROVIDERS = {
  active_provider: 'openai',
  providers: {},
  registered: ['openai', 'deepseek'],
  fallbacks: [
    {
      provider: 'deepseek',
      model: '', base_url: '', model_raw: '', base_url_raw: '',
      api_key_present: false, api_key_redacted: '',
      supports_vision: false, options: {},
    },
    {
      provider: 'openai',
      model: 'backup', base_url: 'https://relay.test/v1',
      model_raw: 'backup', base_url_raw: 'https://relay.test/v1',
      api_key_present: true, api_key_redacted: 'sk-…xyz',
      supports_vision: true, options: { reasoning_effort: 'high' },
    },
  ],
  node_fallbacks: {
    'qq.vision': [],
  },
};

const NODES = [{ id: 'qq.vision', provider: 'openai' }, { id: 'qq.chat', provider: 'openai' }];

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token' });
  vi.mocked(getProviderProfiles).mockResolvedValue({ options: CATALOG, wireFormats: {}, hostProfiles: {} } as any);
  vi.mocked(getProviders).mockResolvedValue(structuredClone(PROVIDERS) as any);
  vi.mocked(getNodes).mockResolvedValue(NODES as any);
  vi.mocked(getRuntimeRaw).mockResolvedValue('providers:\n  openai:\n    timeout_sec: 600.0\n');
  vi.mocked(getNodeRaw).mockResolvedValue('id: qq.vision\nprovider: openai\n');
  vi.mocked(updateFallbacks).mockResolvedValue(structuredClone(PROVIDERS) as any);
  vi.mocked(updateNodeFallbacks).mockResolvedValue(structuredClone(PROVIDERS) as any);
  vi.mocked(clearNodeFallbacks).mockResolvedValue(structuredClone(PROVIDERS) as any);
});

const saveChain = () => fireEvent.click(screen.getByRole('button', { name: '保存备选链' }));
const rows = () => screen.getAllByRole('listitem');
// 标题是静态的，等到它的时候数据还在路上。等列表出现才算加载完。
const ready = () => screen.findAllByRole('listitem');

describe('备选链', () => {
  it('把已配的两条都摆出来', async () => {
    render(<ModelsPage />);
    await ready();
    expect(rows()).toHaveLength(2);
    expect((within(rows()[1]).getByLabelText('模型') as HTMLInputElement).value).toBe('backup');
  });

  it('密钥只显示状态，不回填明文', async () => {
    render(<ModelsPage />);
    await ready();
    const key = within(rows()[1]).getByLabelText('密钥') as HTMLInputElement;
    expect(key.value).toBe('');
    expect(key.placeholder).toContain('已设置');
    expect(key.type).toBe('password');
  });

  it('没动过就存不了，避免空存一次反而改坏', async () => {
    render(<ModelsPage />);
    await ready();
    expect((screen.getByRole('button', { name: '保存备选链' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('改一个字段时，密钥不提交、原始位置照带', async () => {
    render(<ModelsPage />);
    await ready();
    fireEvent.change(within(rows()[1]).getByLabelText('模型'), { target: { value: 'backup-v2' } });
    saveChain();

    await waitFor(() => expect(updateFallbacks).toHaveBeenCalled());
    const sent = vi.mocked(updateFallbacks).mock.calls[0][1];
    expect(sent[1].model).toBe('backup-v2');
    expect(sent[1]._origin).toBe(1);
    // 没填密钥就不该出现这个键——出现了就等于把原密钥覆盖成空。
    expect('api_key' in sent[1]).toBe(false);
    expect(sent[1].supports_vision).toBe(true);
    expect(sent[1].options).toEqual({ reasoning_effort: 'high' });
  });

  it('填了密钥才提交它', async () => {
    render(<ModelsPage />);
    await ready();
    fireEvent.change(within(rows()[1]).getByLabelText('密钥'), { target: { value: 'sk-new' } });
    saveChain();

    await waitFor(() => expect(updateFallbacks).toHaveBeenCalled());
    expect(vi.mocked(updateFallbacks).mock.calls[0][1][1].api_key).toBe('sk-new');
  });

  it('换顺序时原始位置跟着条目走', async () => {
    render(<ModelsPage />);
    await ready();
    fireEvent.click(within(rows()[1]).getByRole('button', { name: '上移' }));
    saveChain();

    await waitFor(() => expect(updateFallbacks).toHaveBeenCalled());
    const sent = vi.mocked(updateFallbacks).mock.calls[0][1];
    expect(sent[0]._origin).toBe(1);
    expect(sent[1]._origin).toBe(0);
  });

  it('新加的条目不带原始位置', async () => {
    render(<ModelsPage />);
    await ready();
    fireEvent.click(screen.getByRole('button', { name: '添加备选' }));
    saveChain();

    await waitFor(() => expect(updateFallbacks).toHaveBeenCalled());
    const sent = vi.mocked(updateFallbacks).mock.calls[0][1];
    expect(sent).toHaveLength(3);
    expect(sent[2]._origin).toBeUndefined();
  });

  it('删掉一条只删那一条', async () => {
    render(<ModelsPage />);
    await ready();
    fireEvent.click(within(rows()[0]).getByRole('button', { name: '删除' }));
    saveChain();

    await waitFor(() => expect(updateFallbacks).toHaveBeenCalled());
    const sent = vi.mocked(updateFallbacks).mock.calls[0][1];
    expect(sent).toHaveLength(1);
    expect(sent[0]._origin).toBe(1);
  });

  it('每条能单独调参数', async () => {
    render(<ModelsPage />);
    await ready();
    fireEvent.click(within(rows()[0]).getByRole('button', { name: /参数/ }));
    fireEvent.click(within(rows()[0]).getByRole('button', { name: '关' }));
    saveChain();

    await waitFor(() => expect(updateFallbacks).toHaveBeenCalled());
    expect(vi.mocked(updateFallbacks).mock.calls[0][1][0].options).toEqual({ thinking: false });
  });

  it('所见即所存：选了哪一档就存哪一档，再点一次才是清空', async () => {
    // 不把「选中的值恰好等于默认值」当成清空 —— tool_choice: auto、thinking: true
    // 这类默认值是真要发出去的，一刀切会把它们静默抹掉。
    render(<ModelsPage />);
    await ready();
    fireEvent.click(within(rows()[1]).getByRole('button', { name: /参数/ }));

    fireEvent.click(within(rows()[1]).getByRole('button', { name: '不指定' }));
    saveChain();
    await waitFor(() => expect(updateFallbacks).toHaveBeenCalled());
    expect(vi.mocked(updateFallbacks).mock.calls[0][1][1].options)
      .toEqual({ reasoning_effort: 'default' });
  });

  it('再点同一档等于取消选中，这一项彻底不配', async () => {
    render(<ModelsPage />);
    await ready();
    fireEvent.click(within(rows()[1]).getByRole('button', { name: /参数/ }));

    fireEvent.click(within(rows()[1]).getByRole('button', { name: '不指定' }));
    fireEvent.click(within(rows()[1]).getByRole('button', { name: '不指定' }));
    saveChain();

    await waitFor(() => expect(updateFallbacks).toHaveBeenCalled());
    expect(vi.mocked(updateFallbacks).mock.calls[0][1][1].options).toEqual({});
  });

  it('换渠道会清掉上一家的参数——键名各家不通用', async () => {
    render(<ModelsPage />);
    await ready();
    fireEvent.change(within(rows()[1]).getByLabelText('渠道'), { target: { value: 'deepseek' } });
    saveChain();

    await waitFor(() => expect(updateFallbacks).toHaveBeenCalled());
    const sent = vi.mocked(updateFallbacks).mock.calls[0][1];
    expect(sent[1].provider).toBe('deepseek');
    expect(sent[1].options).toEqual({});
  });

  it('没勾支持图片的条目会提示带图请求会跳过它', async () => {
    render(<ModelsPage />);
    await ready();
    expect(within(rows()[0]).getByText(/带图的请求会跳过/)).toBeTruthy();
    expect(within(rows()[1]).queryByText(/带图的请求会跳过/)).toBeNull();
  });
});

describe('按节点的备选链', () => {
  const openNode = async (id: string) => {
    render(<ModelsPage />);
    fireEvent.click(await screen.findByRole('button', { name: id }));
    // 节点链可以是空的，这里不能等列表；等档位按钮才是这一块真的画好了。
    await screen.findByRole('button', { name: '跟随全局' });
  };

  it('没配过的节点显示为跟随全局', async () => {
    await openNode('qq.chat');
    expect(screen.getByRole('button', { name: '跟随全局' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText(/现在跟随全局链（2 条）/)).toBeTruthy();
  });

  it('空列表是「禁用」，不是「跟随全局」', async () => {
    await openNode('qq.vision');
    expect(screen.getByRole('button', { name: '禁用' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: '跟随全局' }).getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByText(/直接报错/)).toBeTruthy();
  });

  it('切回跟随全局走的是删除，而不是存一个空链', async () => {
    await openNode('qq.vision');
    fireEvent.click(screen.getByRole('button', { name: '跟随全局' }));
    await waitFor(() => expect(clearNodeFallbacks).toHaveBeenCalledWith('admin-token', 'qq.vision'));
    expect(updateNodeFallbacks).not.toHaveBeenCalled();
  });

  it('禁用某个节点存的是空列表', async () => {
    await openNode('qq.chat');
    fireEvent.click(screen.getByRole('button', { name: '禁用' }));
    await waitFor(() => expect(updateNodeFallbacks).toHaveBeenCalledWith('admin-token', 'qq.chat', []));
  });

  it('节点的链存到节点端点，不会误写全局链', async () => {
    await openNode('qq.chat');
    fireEvent.click(screen.getByRole('button', { name: '自定义' }));
    await waitFor(() => expect(updateNodeFallbacks).toHaveBeenCalled());
    const [, nodeId, chain] = vi.mocked(updateNodeFallbacks).mock.calls[0];
    expect(nodeId).toBe('qq.chat');
    expect(chain).toHaveLength(1);
    expect(updateFallbacks).not.toHaveBeenCalled();
  });
});
