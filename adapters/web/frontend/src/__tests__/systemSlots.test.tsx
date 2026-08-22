// 系统槽位。生图那两个的格式写死在工具源码里，所以不该给它们渠道选项；
// 其余的换家必须连渠道一起改，只换地址会按主渠道的格式发出去。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getSystemModels, updateSystemModel } from '../api/supervisorClient';
import { SystemSlots } from '../console/SystemSlots';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getSystemModels: vi.fn(),
  updateSystemModel: vi.fn(),
}));

const slot = (over: Record<string, unknown>) => ({
  key: 'x', label: 'X', desc: '说明', supports_provider: false, env_prefix: 'CLONOTH_X',
  model: '', base_url: '', model_raw: '', base_url_raw: '', provider: '',
  api_key_present: false, api_key_redacted: '', ...over,
});

const SLOTS = {
  slots: [
    slot({
      key: 'compact', label: '上下文压缩', supports_provider: true,
      model: 'gemini-3.5-flash', model_raw: '${COMPACT_MODEL}',
      api_key_present: true, api_key_redacted: '****abcd',
    }),
    slot({ key: 'summary', label: '轮摘要', supports_provider: true }),
    slot({ key: 'intent', label: '接话意愿', supports_provider: true }),
    slot({ key: 'image', label: '读图', supports_provider: true }),
    slot({ key: 'image_gpt', label: '生图（GPT）', base_url: 'https://img.example/v1', base_url_raw: 'https://img.example/v1' }),
    slot({ key: 'image_gemini', label: '生图（Gemini）' }),
  ],
};

const WIRES = ['openai', 'anthropic', 'deepseek'];
const CHANNELS = [
  { value: 'gemini-中转A', label: 'gemini-中转A · 便宜那个', wire: 'gemini' },
  { value: 'deepseek', label: 'deepseek', wire: 'deepseek' },
];

const PROFILES = {
  options: {},
  wireFormats: { openai: 'openai', deepseek: 'openai', anthropic: 'anthropic' },
  hostProfiles: { 'api.anthropic.com': ['anthropic'], 'api.deepseek.com': ['openai'] },
};

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token' });
  vi.mocked(getSystemModels).mockResolvedValue(structuredClone(SLOTS) as any);
  vi.mocked(updateSystemModel).mockResolvedValue(structuredClone(SLOTS) as any);
});

const rows = () => screen.getAllByRole('listitem');
const rowFor = (label: string) => rows().find((row) => within(row).queryByText(label))!;
const save = () => fireEvent.click(screen.getByRole('button', { name: '保存槽位' }));
const render1 = (profiles: any = PROFILES) => render(
  <SystemSlots activeProvider="openai" channels={CHANNELS} profiles={profiles} wires={WIRES} />,
);

describe('系统槽位', () => {
  it('六个槽位都摆出来，两个生图也在', async () => {
    render1();
    await screen.findByText('系统槽位');
    expect(rows()).toHaveLength(6);
    expect(rowFor('生图（GPT）')).toBeTruthy();
    expect(rowFor('生图（Gemini）')).toBeTruthy();
  });

  it('生图那两个没有渠道选择器，其余都有', async () => {
    // 生图工具的请求格式写死在源码里，摆个选项等于骗人。
    render1();
    await screen.findByText('系统槽位');
    expect(within(rowFor('上下文压缩')).getByLabelText('上下文压缩 渠道')).toBeTruthy();
    expect(within(rowFor('读图')).getByLabelText('读图 渠道')).toBeTruthy();
    expect(within(rowFor('生图（GPT）')).queryByLabelText('生图（GPT） 渠道')).toBeNull();
    expect(within(rowFor('生图（Gemini）')).queryByLabelText('生图（Gemini） 渠道')).toBeNull();
  });

  it('回填的是变量引用原文', async () => {
    render1();
    await screen.findByText('系统槽位');
    const box = within(rowFor('上下文压缩')).getByLabelText('上下文压缩 模型') as HTMLInputElement;
    expect(box.value).toBe('${COMPACT_MODEL}');
    expect(screen.getByText('解析为 gemini-3.5-flash')).toBeTruthy();
  });

  it('密钥只显示状态', async () => {
    render1();
    await screen.findByText('系统槽位');
    const key = within(rowFor('上下文压缩')).getByLabelText('上下文压缩 密钥') as HTMLInputElement;
    expect(key.value).toBe('');
    expect(key.type).toBe('password');
    expect(key.placeholder).toContain('****abcd');
    expect((within(rowFor('读图')).getByLabelText('读图 密钥') as HTMLInputElement).placeholder)
      .toBe('跟随主渠道');
  });

  it('没动过就存不了', async () => {
    render1();
    await screen.findByText('系统槽位');
    expect((screen.getByRole('button', { name: '保存槽位' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('保存', () => {
  it('只提交改过的那一个槽位', async () => {
    render1();
    await screen.findByText('系统槽位');
    fireEvent.change(within(rowFor('生图（Gemini）')).getByLabelText('生图（Gemini） 模型'), {
      target: { value: 'gemini-4-image' },
    });
    save();

    await waitFor(() => expect(updateSystemModel).toHaveBeenCalledTimes(1));
    const [, key, body] = vi.mocked(updateSystemModel).mock.calls[0];
    expect(key).toBe('image_gemini');
    expect(body.model).toBe('gemini-4-image');
    expect('api_key' in body).toBe(false);
  });

  it('填了密钥才提交它', async () => {
    render1();
    await screen.findByText('系统槽位');
    fireEvent.change(within(rowFor('生图（GPT）')).getByLabelText('生图（GPT） 密钥'), {
      target: { value: 'sk-img' },
    });
    save();

    await waitFor(() => expect(updateSystemModel).toHaveBeenCalled());
    expect(vi.mocked(updateSystemModel).mock.calls[0][2].api_key).toBe('sk-img');
  });

  it('清空字段提交空串，那是「删掉这一项」的信号', async () => {
    render1();
    await screen.findByText('系统槽位');
    fireEvent.change(within(rowFor('生图（GPT）')).getByLabelText('生图（GPT） 地址'), {
      target: { value: '' },
    });
    save();

    await waitFor(() => expect(updateSystemModel).toHaveBeenCalled());
    expect(vi.mocked(updateSystemModel).mock.calls[0][2].base_url).toBe('');
  });

  it('生图槽位提交的 provider 恒为空', async () => {
    render1();
    await screen.findByText('系统槽位');
    fireEvent.change(within(rowFor('生图（GPT）')).getByLabelText('生图（GPT） 模型'), {
      target: { value: 'gpt-image-3' },
    });
    save();

    await waitFor(() => expect(updateSystemModel).toHaveBeenCalled());
    expect(vi.mocked(updateSystemModel).mock.calls[0][2].provider).toBe('');
  });

  it('引擎槽位能选渠道并提交', async () => {
    render1();
    await screen.findByText('系统槽位');
    fireEvent.change(within(rowFor('接话意愿')).getByLabelText('接话意愿 渠道'), {
      target: { value: 'deepseek' },
    });
    save();

    await waitFor(() => expect(updateSystemModel).toHaveBeenCalled());
    const [, key, body] = vi.mocked(updateSystemModel).mock.calls[0];
    expect(key).toBe('intent');
    expect(body.provider).toBe('deepseek');
  });
});

describe('换家提示', () => {
  it('引擎槽位填了地址却没选渠道时提醒', async () => {
    render1();
    await screen.findByText('系统槽位');
    fireEvent.change(within(rowFor('轮摘要')).getByLabelText('轮摘要 地址'), {
      target: { value: 'https://api.anthropic.com' },
    });
    expect(within(rowFor('轮摘要')).getByText(/换家要连渠道一起选/)).toBeTruthy();
  });

  it('选了渠道就不再提醒', async () => {
    render1();
    await screen.findByText('系统槽位');
    const row = rowFor('轮摘要');
    fireEvent.change(within(row).getByLabelText('轮摘要 地址'), { target: { value: 'https://x/v1' } });
    fireEvent.change(within(row).getByLabelText('轮摘要 渠道'), { target: { value: 'anthropic' } });
    expect(within(rowFor('轮摘要')).queryByText(/换家要连渠道一起选/)).toBeNull();
  });

  it('生图槽位改地址时说明格式是固定的', async () => {
    render1();
    await screen.findByText('系统槽位');
    expect(within(rowFor('生图（GPT）')).getByText(/格式固定/)).toBeTruthy();
  });

  it('选了渠道又填了别家的地址时点出来', async () => {
    render1();
    await screen.findByText('系统槽位');
    const row = rowFor('轮摘要');
    fireEvent.change(within(row).getByLabelText('轮摘要 渠道'), { target: { value: 'openai' } });
    fireEvent.change(within(row).getByLabelText('轮摘要 地址'), {
      target: { value: 'https://api.anthropic.com' },
    });

    expect(within(rowFor('轮摘要')).getByText(/这个地址是 anthropic 的/)).toBeTruthy();
  });

  it('跟随主渠道时不猜格式', async () => {
    // provider 留空表示跟随，最终发哪种格式这里不知道，猜不了就别猜。
    render1();
    await screen.findByText('系统槽位');
    fireEvent.change(within(rowFor('轮摘要')).getByLabelText('轮摘要 地址'), {
      target: { value: 'https://api.anthropic.com' },
    });

    expect(within(rowFor('轮摘要')).queryByText(/这个地址是/)).toBeNull();
  });
});
