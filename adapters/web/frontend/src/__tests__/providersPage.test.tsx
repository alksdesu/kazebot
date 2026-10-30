// 渠道页。读到的是脱敏视图，最要紧的是「界面看不见的东西不能被存没」：
// 内联密钥不回填，变量引用要按原文回填，新建的块存盘前不能先落到 config.yaml。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  deleteProvider,
  getNodes,
  getProviderProfiles,
  getProviders,
  getRuntimeFile,
  ReceiptUnreadable,
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
  getRuntimeFile: vi.fn(),
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
  reserved_names: ['fallbacks', 'image_gen', 'node_fallbacks', 'platforms', 'provider', 'system_models', 'version'],
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
  vi.mocked(getRuntimeFile).mockResolvedValue({ content: 'routing:\n  vision:\n    enabled: auto\n', hash: 'runtime-1', source: 'override', path: 'config/runtime.yaml' });
  vi.mocked(upsertProvider).mockResolvedValue(structuredClone(PROVIDERS) as any);
  vi.mocked(setActiveProvider).mockResolvedValue(structuredClone(PROVIDERS) as any);
  vi.mocked(deleteProvider).mockResolvedValue(structuredClone(PROVIDERS) as any);
});

const rows = () => screen.getAllByRole('listitem');
// 标题是静态的，等到它的时候数据还在路上。等列表出现才算加载完。
const ready = () => screen.findAllByRole('listitem');
// 认标题里的渠道名，不能按行内文本找：每行的格式下拉都列着全部家族名。
const rowFor = (name: string) => rows().find(
  (row) => within(row).getByRole('heading').textContent === name,
)!;
const saveChain = () => fireEvent.click(screen.getByRole('button', { name: '保存渠道' }));

describe('渠道列表', () => {
  it('把配好的渠道都摆出来，并标出在用的那个', async () => {
    render(<ProvidersPage />);
    await ready();
    expect(rows()).toHaveLength(2);
    expect(within(rowFor('openai')).getByText('在用')).toBeTruthy();
    expect(within(rowFor('anthropic')).getByRole('button', { name: '设为活跃' })).toBeTruthy();
  });

  it('回填的是变量引用原文，不是展开后的值', async () => {
    // 回填展开值再保存，就把这层间接烧死在 config.yaml 里了。
    render(<ProvidersPage />);
    await ready();
    const model = within(rowFor('openai')).getByLabelText('openai 模型') as HTMLInputElement;
    expect(model.value).toBe('${OPENAI_MODEL}');
    expect(model.value).not.toBe('gpt-5.4');
  });

  it('密钥只显示状态，不回填明文', async () => {
    render(<ProvidersPage />);
    await ready();
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
    await ready();
    expect((screen.getByRole('button', { name: '保存渠道' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('保存', () => {
  it('改模型时不提交密钥', async () => {
    render(<ProvidersPage />);
    await ready();
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
    await ready();
    fireEvent.change(within(rowFor('anthropic')).getByLabelText('anthropic 密钥'), {
      target: { value: 'sk-ant-new' },
    });
    saveChain();

    await waitFor(() => expect(upsertProvider).toHaveBeenCalled());
    expect(vi.mocked(upsertProvider).mock.calls[0][2].api_key).toBe('sk-ant-new');
  });

  it('只提交改过的那一个，没动的渠道不重写', async () => {
    render(<ProvidersPage />);
    await ready();
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
    await ready();
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

  it('新建的渠道整份提交，没填的字段也带着空串 —— 后端要靠它把块建全', async () => {
    render(<ProvidersPage />);
    await ready();
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'deepseek' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
    fireEvent.change(within(rowFor('deepseek')).getByLabelText('deepseek 模型'), {
      target: { value: 'deepseek-v4-pro' },
    });
    saveChain();

    await waitFor(() => expect(upsertProvider).toHaveBeenCalledTimes(1));
    expect(vi.mocked(upsertProvider).mock.calls[0][2]).toEqual({
      model: 'deepseek-v4-pro', base_url: '', supports_vision: 'auto', label: '', type: 'deepseek',
    });
  });

  it('每种格式都能再开一个，已经配过的也不例外', async () => {
    // 同一家挂两个中转是常态，可选格式不该因为配过一次就少一项。
    render(<ProvidersPage />);
    await ready();
    const options = within(screen.getByLabelText('新渠道格式')).getAllByRole('option');

    expect(options.map((option) => (option as HTMLOptionElement).value))
      .toEqual(['', 'openai', 'anthropic', 'deepseek']);
  });

  it('同一格式的第二个渠道自己起名，格式一起提交', async () => {
    render(<ProvidersPage />);
    await ready();
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
    await ready();
    fireEvent.change(screen.getByLabelText('新渠道名'), { target: { value: 'openai' } });
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'anthropic' } });

    expect((screen.getByRole('button', { name: '添加' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/已经有一个叫 openai 的渠道/)).toBeTruthy();
  });

  it('配置文件的保留段名不能当渠道名', async () => {
    // 渠道块和这些段同在 config/models.yaml 顶层，起这种名字一保存就把那一段改坏了。
    render(<ProvidersPage />);
    await ready();
    fireEvent.change(screen.getByLabelText('新渠道名'), { target: { value: 'platforms' } });
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'openai' } });

    expect((screen.getByRole('button', { name: '添加' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/platforms 是 config\/models\.yaml 里的保留段名/)).toBeTruthy();
  });

  it('名字只是含着保留字的照样能加', async () => {
    render(<ProvidersPage />);
    await ready();
    fireEvent.change(screen.getByLabelText('新渠道名'), { target: { value: 'platforms-relay' } });
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'openai' } });

    expect((screen.getByRole('button', { name: '添加' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByText(/保留段名/)).toBeNull();
  });

  it('还没保存的渠道不能设为活跃', async () => {
    render(<ProvidersPage />);
    await ready();
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'deepseek' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));

    const button = within(rowFor('deepseek')).getByRole('button', { name: '设为活跃' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it('删掉还没保存的渠道只是丢掉草稿，不打后端', async () => {
    render(<ProvidersPage />);
    await ready();
    fireEvent.change(screen.getByLabelText('新渠道格式'), { target: { value: 'deepseek' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
    fireEvent.click(within(rowFor('deepseek')).getByRole('button', { name: '删除' }));

    expect(rows()).toHaveLength(2);
    expect(deleteProvider).not.toHaveBeenCalled();
  });

  it('正在用的渠道不能删', async () => {
    render(<ProvidersPage />);
    await ready();
    const button = within(rowFor('openai')).getByRole('button', { name: '删除' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('切换活跃渠道', () => {
  it('点一下就切，不用先保存', async () => {
    render(<ProvidersPage />);
    await ready();
    fireEvent.click(within(rowFor('anthropic')).getByRole('button', { name: '设为活跃' }));
    await waitFor(() => expect(setActiveProvider).toHaveBeenCalledWith('admin-token', 'anthropic'));
    expect(upsertProvider).not.toHaveBeenCalled();
  });

  it('提示哪些节点写死了自己的渠道，切换不影响它们', async () => {
    render(<ProvidersPage />);
    await ready();
    const hint = await screen.findByText(/写死了自己的渠道/);
    expect(hint.textContent).toContain('qq.vision');
    expect(hint.textContent).toContain('draw.image_gen');
    // 没写 provider 的节点会跟随，不该出现在这份名单里。
    expect(hint.textContent).not.toContain('qq.orchestrator');
  });

  it('取不到节点清单时说出来，页面照常能用，重试读到了就补上名单', async () => {
    vi.mocked(getNodes).mockRejectedValueOnce(new Error('boom'));
    render(<ProvidersPage />);
    await ready();
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('节点表读取失败');
    expect(alert.textContent).toContain('boom');
    expect(screen.queryByText(/这 \d+ 个节点写死了自己的渠道/)).toBeNull();
    expect(rows()).toHaveLength(2);

    fireEvent.click(within(alert).getByRole('button', { name: '重试' }));

    const hint = await screen.findByText(/这 \d+ 个节点写死了自己的渠道/);
    expect(hint.textContent).toContain('qq.vision');
    expect(screen.queryByText(/节点表读取失败/)).toBeNull();
    // 重试只补提示，不重读渠道：重读会把没保存的草稿冲掉
    expect(getProviders).toHaveBeenCalledTimes(1);
  });

  it('重试提示时不冲掉没保存的草稿', async () => {
    vi.mocked(getNodes).mockRejectedValueOnce(new Error('boom'));
    render(<ProvidersPage />);
    await ready();
    fireEvent.change(within(rowFor('anthropic')).getByLabelText('anthropic 模型'), { target: { value: 'unsaved-model' } });

    fireEvent.click(within(await screen.findByRole('alert')).getByRole('button', { name: '重试' }));

    await screen.findByText(/这 \d+ 个节点写死了自己的渠道/);
    expect((within(rowFor('anthropic')).getByLabelText('anthropic 模型') as HTMLInputElement).value).toBe('unsaved-model');
  });
});

describe('这个渠道看不看得了图', () => {
  it('没配时跟随这家的默认，并把推断结果写在选项里', async () => {
    render(<ProvidersPage />);
    await ready();
    const picker = within(rowFor('openai')).getByLabelText('openai 读图能力') as HTMLSelectElement;

    expect(picker.value).toBe('auto');
    expect(await within(rowFor('openai')).findByText(/跟随 openai 默认（能看图）/)).toBeTruthy();
  });

  it('各格式的默认值没读到时只说跟随默认，不替它说能看图', async () => {
    vi.mocked(getProviderProfiles).mockRejectedValue(new Error('401'));
    render(<ProvidersPage />);
    await ready();

    expect((await screen.findByRole('alert')).textContent).toContain('各格式的默认值读取失败');
    const auto = within(within(rowFor('openai')).getByLabelText('openai 读图能力')).getByRole('option', { name: /跟随/ });
    expect(auto.textContent).toBe('跟随 openai 默认');
  });

  it('默认值表里没有这个格式时也不替它下结论', async () => {
    vi.mocked(getProviderProfiles).mockResolvedValue({ ...structuredClone(PROFILES), defaultVision: { anthropic: true } } as any);
    render(<ProvidersPage />);
    await ready();
    // anthropic 那行有了结论，说明默认值表已经到了
    await within(rowFor('anthropic')).findByText(/跟随 anthropic 默认（能看图）/);

    const auto = within(within(rowFor('openai')).getByLabelText('openai 读图能力')).getByRole('option', { name: /跟随/ });
    expect(auto.textContent).toBe('跟随 openai 默认');
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
    await ready();

    expect(await screen.findByText(/跟随 deepseek 默认（看不了图）/)).toBeTruthy();
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
    await ready();

    expect((within(rowFor('openai')).getByLabelText('openai 读图能力') as HTMLSelectElement).value)
      .toBe('no');
  });

  it('改了档位会跟着保存提交上去', async () => {
    render(<ProvidersPage />);
    await ready();
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
    await ready();
    fireEvent.change(within(rowFor('openai')).getByLabelText('openai 地址'), {
      target: { value: 'https://api.anthropic.com' },
    });

    expect(within(rowFor('openai')).getByText(/这个地址是 anthropic 的/)).toBeTruthy();
    // 提示不是拦截：中转站认不出，硬拦会误伤。
    expect((screen.getByRole('button', { name: '保存渠道' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('原样的官方地址不触发提醒', async () => {
    render(<ProvidersPage />);
    await ready();

    expect(screen.queryByText(/这个地址是/)).toBeNull();
  });

  it('取不到那份清单就不提醒，但说出核对不了', async () => {
    vi.mocked(getProviderProfiles).mockRejectedValue(new Error('401'));
    render(<ProvidersPage />);
    await ready();
    fireEvent.change(within(rowFor('openai')).getByLabelText('openai 地址'), {
      target: { value: 'https://api.anthropic.com' },
    });

    expect(screen.queryByText(/这个地址是/)).toBeNull();
    expect((await screen.findByRole('alert')).textContent).toContain('核对不了地址');
  });
});

describe('写回执读不懂', () => {
  it('存上了、回执读不懂时重读渠道表顶上，回执的事单独交代', async () => {
    const saved = structuredClone(PROVIDERS);
    saved.providers.openai.model_raw = 'gpt-5.5';
    vi.mocked(upsertProvider).mockRejectedValueOnce(new ReceiptUnreadable('渠道 openai 已经存上，可回执格式不对：缺 registered'));
    vi.mocked(getProviders).mockResolvedValueOnce(structuredClone(PROVIDERS) as any).mockResolvedValueOnce(saved as any);
    render(<ProvidersPage />);
    await ready();
    fireEvent.change(within(rowFor('openai')).getByLabelText('openai 模型'), { target: { value: 'gpt-5.5' } });
    saveChain();

    expect(await screen.findByText('渠道 openai 已经存上，可回执格式不对：缺 registered')).toBeInTheDocument();
    expect(screen.getByText(/已保存。engine 每个任务重新取一次密钥/)).toBeInTheDocument();
    // 重读回来的那份就是存上的：没有未保存修改了。
    expect(within(rowFor('openai')).getByLabelText('openai 模型')).toHaveValue('gpt-5.5');
    expect(screen.getByRole('button', { name: '保存渠道' })).toBeDisabled();
  });

  it('重读也没成时说清先刷新再改', async () => {
    vi.mocked(setActiveProvider).mockRejectedValueOnce(new ReceiptUnreadable('已经切到渠道 anthropic，可回执格式不对：缺 registered'));
    vi.mocked(getProviders).mockResolvedValueOnce(structuredClone(PROVIDERS) as any).mockRejectedValueOnce(new Error('offline'));
    render(<ProvidersPage />);
    await ready();
    fireEvent.click(within(rowFor('anthropic')).getByRole('button', { name: '设为活跃' }));

    expect(await screen.findByText(
      '已经切到渠道 anthropic，可回执格式不对：缺 registered。刷新页面读入最新内容再改。刚才重新读取也没成：offline',
    )).toBeInTheDocument();
    expect(screen.getByText('已切到 anthropic。')).toBeInTheDocument();
  });
});
