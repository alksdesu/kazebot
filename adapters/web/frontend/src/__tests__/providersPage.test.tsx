// 渠道页。读到的是脱敏视图，最要紧的是「界面看不见的东西不能被存没」：
// 内联密钥不回填，变量引用要按原文回填，新建的块存盘前不能先落到 config.yaml。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  deleteProvider,
  getNodes,
  getProviderProfiles,
  getProviders,
  getRuntimeRaw,
  setActiveProvider,
  upsertProvider,
} from '../api/supervisorClient';
import { ProvidersPage } from '../console/ProvidersPage';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  deleteProvider: vi.fn(),
  getNodes: vi.fn(),
  getProviderProfiles: vi.fn(),
  getProviders: vi.fn(),
  getRuntimeRaw: vi.fn(),
  setActiveProvider: vi.fn(),
  upsertProvider: vi.fn(),
}));

const PROVIDERS = {
  active_provider: 'openai',
  registered: ['openai', 'anthropic', 'deepseek'],
  providers: {
    openai: {
      model: 'gpt-5.4',
      base_url: 'https://api.openai.com/v1',
      model_raw: '${OPENAI_MODEL}',
      base_url_raw: 'https://api.openai.com/v1',
      api_key_present: true,
      api_key_redacted: '****cdef',
    },
    anthropic: {
      model: 'claude-sonnet-4-6',
      base_url: 'https://api.anthropic.com',
      model_raw: 'claude-sonnet-4-6',
      base_url_raw: 'https://api.anthropic.com',
      api_key_present: false,
      api_key_redacted: '',
    },
  },
  fallbacks: [],
  node_fallbacks: {},
};

const PROFILES = {
  options: {},
  wireFormats: { openai: 'openai', deepseek: 'openai', anthropic: 'anthropic' },
  hostProfiles: { 'api.openai.com': ['openai'], 'api.anthropic.com': ['anthropic'] },
  defaultVision: { openai: true, deepseek: false, anthropic: true },
};

const NODES = [
  { id: 'qq.orchestrator' },
  { id: 'qq.vision', provider: 'openai' },
  { id: 'draw.image_gen', provider: 'openai' },
];

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token' });
  vi.mocked(getProviders).mockResolvedValue(structuredClone(PROVIDERS) as any);
  vi.mocked(getNodes).mockResolvedValue(structuredClone(NODES) as any);
  vi.mocked(getProviderProfiles).mockResolvedValue(structuredClone(PROFILES) as any);
  vi.mocked(getRuntimeRaw).mockResolvedValue('routing:\n  vision:\n    enabled: auto\n');
  vi.mocked(upsertProvider).mockResolvedValue(structuredClone(PROVIDERS) as any);
  vi.mocked(setActiveProvider).mockResolvedValue(structuredClone(PROVIDERS) as any);
  vi.mocked(deleteProvider).mockResolvedValue(structuredClone(PROVIDERS) as any);
});

const rows = () => screen.getAllByRole('listitem');
// 认标题里的渠道名，不能按行内文本找：每行的格式下拉都列着全部家族名。
const rowFor = (name: string) => rows().find(
  (row) => row.querySelector('.qc-cap-name')?.textContent === name,
)!;
const saveChain = () => fireEvent.click(screen.getByRole('button', { name: '保存渠道' }));

describe('渠道列表', () => {
  it('把配好的渠道都摆出来，并标出在用的那个', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    expect(rows()).toHaveLength(2);
    expect(within(rowFor('openai')).getByText('在用')).toBeTruthy();
    expect(within(rowFor('anthropic')).getByRole('button', { name: '设为活跃' })).toBeTruthy();
  });

  it('回填的是变量引用原文，不是展开后的值', async () => {
    // 回填展开值再保存，就把这层间接烧死在 config.yaml 里了。
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    const model = within(rowFor('openai')).getByLabelText('openai 模型') as HTMLInputElement;
    expect(model.value).toBe('${OPENAI_MODEL}');
    expect(model.value).not.toBe('gpt-5.4');
  });

  it('密钥只显示状态，不回填明文', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    const key = within(rowFor('openai')).getByLabelText('openai 密钥') as HTMLInputElement;
    expect(key.value).toBe('');
    expect(key.type).toBe('password');
    expect(key.placeholder).toContain('****cdef');
    expect(within(rowFor('anthropic')).getByLabelText('anthropic 密钥')).toHaveProperty(
      'placeholder', '未设置',
    );
  });

  it('没动过就存不了', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    expect((screen.getByRole('button', { name: '保存渠道' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('保存', () => {
  it('改模型时不提交密钥', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(within(rowFor('openai')).getByLabelText('openai 模型'), {
      target: { value: 'gpt-5.5' },
    });
    saveChain();

    await waitFor(() => expect(upsertProvider).toHaveBeenCalled());
    const [, name, body] = vi.mocked(upsertProvider).mock.calls[0];
    expect(name).toBe('openai');
    expect(body.model).toBe('gpt-5.5');
    // 出现这个键就等于把原密钥覆盖成空。
    expect('api_key' in body).toBe(false);
  });

  it('填了密钥才提交它', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(within(rowFor('anthropic')).getByLabelText('anthropic 密钥'), {
      target: { value: 'sk-ant-new' },
    });
    saveChain();

    await waitFor(() => expect(upsertProvider).toHaveBeenCalled());
    expect(vi.mocked(upsertProvider).mock.calls[0][2].api_key).toBe('sk-ant-new');
  });

  it('只提交改过的那一个，没动的渠道不重写', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(within(rowFor('anthropic')).getByLabelText('anthropic 模型'), {
      target: { value: 'claude-opus-4-2' },
    });
    saveChain();

    await waitFor(() => expect(upsertProvider).toHaveBeenCalledTimes(1));
    expect(vi.mocked(upsertProvider).mock.calls[0][1]).toBe('anthropic');
  });
});

describe('新增与删除', () => {
  it('新加的渠道先留在本地，保存时才建块', async () => {
    // 后端认不出空块，先建会让它从列表里消失。
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'deepseek' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));

    expect(rows()).toHaveLength(3);
    expect(upsertProvider).not.toHaveBeenCalled();

    fireEvent.change(within(rowFor('deepseek')).getByLabelText('deepseek 模型'), {
      target: { value: 'deepseek-v4-pro' },
    });
    saveChain();
    await waitFor(() => expect(upsertProvider).toHaveBeenCalledTimes(1));
    expect(vi.mocked(upsertProvider).mock.calls[0][1]).toBe('deepseek');
  });

  it('每种格式都能再开一个，已经配过的也不例外', async () => {
    // 同一家挂两个中转是常态，可选格式不该因为配过一次就少一项。
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    const options = within(screen.getByLabelText('新渠道格式')).getAllByRole('option');

    expect(options.map((option) => (option as HTMLOptionElement).value))
      .toEqual(['', 'openai', 'anthropic', 'deepseek']);
  });

  it('同一格式的第二个渠道自己起名，格式一起提交', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(screen.getByLabelText('新渠道名'), { target: { value: 'openai-relay' } });
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'openai' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));

    fireEvent.change(within(rowFor('openai-relay')).getByLabelText('openai-relay 模型'), {
      target: { value: 'gpt-5.5' },
    });
    saveChain();

    await waitFor(() => expect(upsertProvider).toHaveBeenCalledTimes(1));
    const [, name, body] = vi.mocked(upsertProvider).mock.calls[0];
    expect(name).toBe('openai-relay');
    // 名字猜不出家族，不带上 type 后端只能拒。
    expect(body.type).toBe('openai');
  });

  it('重名的渠道加不进去', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(screen.getByLabelText('新渠道名'), { target: { value: 'openai' } });
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'anthropic' } });

    expect((screen.getByRole('button', { name: '添加' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/已经有一个叫 openai 的渠道/)).toBeTruthy();
  });

  it('还没保存的渠道不能设为活跃', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'deepseek' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));

    const button = within(rowFor('deepseek')).getByRole('button', { name: '设为活跃' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it('删掉还没保存的渠道只是丢掉草稿，不打后端', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'deepseek' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
    fireEvent.click(within(rowFor('deepseek')).getByRole('button', { name: '删除' }));

    expect(rows()).toHaveLength(2);
    expect(deleteProvider).not.toHaveBeenCalled();
  });

  it('正在用的渠道不能删', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    const button = within(rowFor('openai')).getByRole('button', { name: '删除' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('切换活跃渠道', () => {
  it('点一下就切，不用先保存', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.click(within(rowFor('anthropic')).getByRole('button', { name: '设为活跃' }));
    await waitFor(() => expect(setActiveProvider).toHaveBeenCalledWith('admin-token', 'anthropic'));
    expect(upsertProvider).not.toHaveBeenCalled();
  });

  it('提示哪些节点写死了自己的渠道，切换不影响它们', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    const hint = await screen.findByText(/写死了自己的渠道/);
    expect(hint.textContent).toContain('qq.vision');
    expect(hint.textContent).toContain('draw.image_gen');
    // 没写 provider 的节点会跟随，不该出现在这份名单里。
    expect(hint.textContent).not.toContain('qq.orchestrator');
  });

  it('取不到节点清单时不提示，但页面照常能用', async () => {
    vi.mocked(getNodes).mockRejectedValue(new Error('boom'));
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    expect(screen.queryByText(/写死了自己的渠道/)).toBeNull();
    expect(rows()).toHaveLength(2);
  });
});

describe('这个渠道看不看得了图', () => {
  it('没配时跟随这家的默认，并把推断结果写在选项里', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    const picker = within(rowFor('openai')).getByLabelText('openai 读图能力') as HTMLSelectElement;

    expect(picker.value).toBe('auto');
    expect(within(rowFor('openai')).getByText(/跟随 openai 默认（能看图）/)).toBeTruthy();
  });

  it('后端说这家默认看不了图就照实显示', async () => {
    vi.mocked(getProviders).mockResolvedValue({
      ...structuredClone(PROVIDERS),
      providers: {
        deepseek: {
          model: 'deepseek-chat', base_url: 'https://api.deepseek.com',
          model_raw: 'deepseek-chat', base_url_raw: 'https://api.deepseek.com',
          api_key_present: true, api_key_redacted: '****9999', supports_vision: null,
        },
      },
    } as any);
    render(<ProvidersPage />);
    await screen.findByText('渠道');

    expect(screen.getByText(/跟随 deepseek 默认（看不了图）/)).toBeTruthy();
  });

  it('显式配过就回填成那一档，不会退回自动', async () => {
    // 「没配」和「配了个 false」分不开的话，存一次就把跟随默认写死了。
    vi.mocked(getProviders).mockResolvedValue({
      ...structuredClone(PROVIDERS),
      providers: {
        openai: { ...structuredClone(PROVIDERS).providers.openai, supports_vision: false },
      },
    } as any);
    render(<ProvidersPage />);
    await screen.findByText('渠道');

    expect((within(rowFor('openai')).getByLabelText('openai 读图能力') as HTMLSelectElement).value)
      .toBe('no');
  });

  it('改了档位会跟着保存提交上去', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(within(rowFor('openai')).getByLabelText('openai 读图能力'), {
      target: { value: 'no' },
    });
    saveChain();

    await waitFor(() => expect(upsertProvider).toHaveBeenCalled());
    expect(vi.mocked(upsertProvider).mock.calls[0][2].supports_vision).toBe('no');
  });
});

describe('地址填成别家的', () => {
  it('填了 anthropic 的官方地址就提醒，但照样存得下去', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(within(rowFor('openai')).getByLabelText('openai 地址'), {
      target: { value: 'https://api.anthropic.com' },
    });

    expect(within(rowFor('openai')).getByText(/这个地址是 anthropic 的/)).toBeTruthy();
    // 提示不是拦截：中转站认不出，硬拦会误伤。
    expect((screen.getByRole('button', { name: '保存渠道' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('原样的官方地址不触发提醒', async () => {
    render(<ProvidersPage />);
    await screen.findByText('渠道');

    expect(screen.queryByText(/这个地址是/)).toBeNull();
  });

  it('取不到那份清单就不提醒', async () => {
    vi.mocked(getProviderProfiles).mockRejectedValue(new Error('401'));
    render(<ProvidersPage />);
    await screen.findByText('渠道');
    fireEvent.change(within(rowFor('openai')).getByLabelText('openai 地址'), {
      target: { value: 'https://api.anthropic.com' },
    });

    expect(screen.queryByText(/这个地址是/)).toBeNull();
  });
});
