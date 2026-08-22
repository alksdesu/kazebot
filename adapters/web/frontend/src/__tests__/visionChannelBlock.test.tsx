// 看图渠道。表情包打标和 read_image 共用它，选错格式的后果是整库打不上标，
// 所以这一块盯的是「选了哪家、按哪家的格式发、模型名问谁要」。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getProviderProfiles,
  getProviders,
  getSystemModels,
  listUpstreamModels,
  updateSystemModel,
} from '../api/supervisorClient';
import { VisionChannelBlock } from '../console/VisionChannelBlock';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getProviderProfiles: vi.fn(),
  getProviders: vi.fn(),
  getSystemModels: vi.fn(),
  listUpstreamModels: vi.fn(),
  updateSystemModel: vi.fn(),
}));

const imageSlot = (over: Record<string, unknown> = {}) => ({
  key: 'image', label: '读图', desc: '给看不了图的模型描述图片内容。',
  supports_provider: true, env_prefix: 'CLONOTH_IMAGE',
  model: '', base_url: '', model_raw: '', base_url_raw: '', provider: '',
  api_key_present: false, api_key_redacted: '', ...over,
});

const slots = (over: Record<string, unknown> = {}) => ({
  slots: [imageSlot(over)], image_tools: [], image_default_channel: '',
});

const PROVIDERS = {
  active_provider: 'gemini',
  providers: { gemini: { model: 'g', base_url: 'https://generativelanguage.googleapis.com', api_key_present: true } },
  fallbacks: [],
  node_fallbacks: {},
  registered: ['anthropic', 'gemini', 'openai'],
};

const PROFILES = {
  options: {},
  wireFormats: { openai: 'openai', anthropic: 'anthropic', gemini: 'gemini' },
  hostProfiles: {
    'api.anthropic.com': ['anthropic'],
    'generativelanguage.googleapis.com': ['gemini'],
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token' });
  vi.mocked(getSystemModels).mockResolvedValue(structuredClone(slots()) as any);
  vi.mocked(updateSystemModel).mockResolvedValue(structuredClone(slots()) as any);
  vi.mocked(getProviders).mockResolvedValue(structuredClone(PROVIDERS) as any);
  vi.mocked(getProviderProfiles).mockResolvedValue(structuredClone(PROFILES) as any);
  vi.mocked(listUpstreamModels).mockResolvedValue(['gemini-3.5-flash', 'gemini-3-pro']);
});

const show = async () => {
  render(<VisionChannelBlock />);
  await screen.findByLabelText('看图渠道类型');
};

const picker = () => screen.getByLabelText('看图渠道类型') as HTMLSelectElement;
const addr = () => screen.getByLabelText('看图渠道地址');
const save = () => fireEvent.click(screen.getByRole('button', { name: '保存渠道' }));

describe('选渠道', () => {
  it('列出全部已注册渠道，并说明留空跟随的是哪一家', async () => {
    await show();
    const names = [...picker().options].map((option) => option.value);
    expect(names).toEqual(['', 'anthropic', 'gemini', 'openai']);
    expect(screen.getByText('跟随主渠道（gemini）')).toBeTruthy();
  });

  it('选了就提交上去', async () => {
    await show();
    fireEvent.change(picker(), { target: { value: 'anthropic' } });
    save();

    await waitFor(() => expect(updateSystemModel).toHaveBeenCalled());
    const [, slot, body] = vi.mocked(updateSystemModel).mock.calls[0];
    expect(slot).toBe('image');
    expect(body.provider).toBe('anthropic');
  });

  it('取不到渠道清单也不挡住保存', async () => {
    // 这两份只用来渲染下拉和提示，supervisor 抽一下不该让人改不了配置。
    vi.mocked(getProviders).mockRejectedValue(new Error('nope'));
    vi.mocked(getProviderProfiles).mockRejectedValue(new Error('nope'));
    render(<VisionChannelBlock />);
    await screen.findByLabelText('看图渠道地址');

    fireEvent.change(addr(), { target: { value: 'https://relay.example/v1' } });
    save();
    await waitFor(() => expect(updateSystemModel).toHaveBeenCalled());
  });
});

describe('格式提示', () => {
  it('填了地址却没选渠道时说清楚会按 OpenAI 发', async () => {
    await show();
    fireEvent.change(addr(), { target: { value: 'https://relay.example/v1' } });
    expect(screen.getByText(/按 OpenAI 格式发/)).toBeTruthy();
  });

  it('选了渠道就不再提醒', async () => {
    await show();
    fireEvent.change(addr(), { target: { value: 'https://relay.example/v1' } });
    fireEvent.change(picker(), { target: { value: 'openai' } });
    expect(screen.queryByText(/按 OpenAI 格式发/)).toBeNull();
  });

  it('地址是别家官方域名时点出来', async () => {
    await show();
    fireEvent.change(picker(), { target: { value: 'openai' } });
    fireEvent.change(addr(), { target: { value: 'https://api.anthropic.com' } });
    expect(screen.getByText(/这个地址是 anthropic 的/)).toBeTruthy();
  });

  it('什么都没填时不提醒', async () => {
    await show();
    expect(screen.queryByText(/按 OpenAI 格式发/)).toBeNull();
  });
});

describe('拉模型', () => {
  it('带上槽位名，后端才拿得到这一槽自己的密钥', async () => {
    // 不带 slot 的话后端只会去翻渠道块，槽位单独配的那把密钥它够不着。
    await show();
    fireEvent.click(screen.getByRole('button', { name: '拉取' }));

    await waitFor(() => expect(listUpstreamModels).toHaveBeenCalled());
    const [, name, body] = vi.mocked(listUpstreamModels).mock.calls[0];
    expect(name).toBe('gemini');
    expect(body.slot).toBe('image');
    expect('api_key' in body).toBe(false);
  });

  it('按选中的渠道问，不是按主渠道', async () => {
    await show();
    fireEvent.change(picker(), { target: { value: 'anthropic' } });
    fireEvent.click(screen.getByRole('button', { name: '拉取' }));

    await waitFor(() => expect(listUpstreamModels).toHaveBeenCalled());
    expect(vi.mocked(listUpstreamModels).mock.calls[0][1]).toBe('anthropic');
  });

  it('自己填了地址而没选渠道时按 OpenAI 问', async () => {
    await show();
    fireEvent.change(addr(), { target: { value: 'https://relay.example/v1' } });
    fireEvent.click(screen.getByRole('button', { name: '拉取' }));

    await waitFor(() => expect(listUpstreamModels).toHaveBeenCalled());
    expect(vi.mocked(listUpstreamModels).mock.calls[0][1]).toBe('openai');
  });

  it('拉到的名字进候选表', async () => {
    await show();
    fireEvent.click(screen.getByRole('button', { name: '拉取' }));
    await screen.findByText('2 个模型，点输入框选');
  });
});

describe('存盘', () => {
  it('没动过就存不了', async () => {
    await show();
    expect((screen.getByRole('button', { name: '保存渠道' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('密钥留空就不提交它，空串是删除信号', async () => {
    vi.mocked(getSystemModels).mockResolvedValue(
      structuredClone(slots({ api_key_present: true, api_key_redacted: '****abcd' })) as any,
    );
    await show();
    fireEvent.change(screen.getByLabelText('看图渠道模型'), { target: { value: 'flash' } });
    save();

    await waitFor(() => expect(updateSystemModel).toHaveBeenCalled());
    expect('api_key' in vi.mocked(updateSystemModel).mock.calls[0][2]).toBe(false);
  });

  it('回填的是变量引用原文', async () => {
    vi.mocked(getSystemModels).mockResolvedValue(structuredClone(slots({
      model: 'gemini-3.5-flash', model_raw: '${VISION_MODEL}', provider: 'gemini',
    })) as any);
    await show();

    expect((screen.getByLabelText('看图渠道模型') as HTMLInputElement).value).toBe('${VISION_MODEL}');
    expect(screen.getByText('解析为 gemini-3.5-flash')).toBeTruthy();
    expect(picker().value).toBe('gemini');
  });
});
