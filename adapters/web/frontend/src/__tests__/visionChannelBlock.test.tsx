// 看图渠道在表情包页只回显。改它的入口只有渠道页的系统槽位一处，
// 两处都能写就会出现两份各自为政的草稿，先保存的那份被后保存的覆盖。
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getSystemModels, updateSystemModel } from '../api/supervisorClient';
import { SYSTEM_SLOTS_ANCHOR } from '../console/SystemSlots';
import { VisionChannelBlock } from '../console/VisionChannelBlock';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getSystemModels: vi.fn(),
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

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token' });
  useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'qq-stickers' });
  window.history.replaceState(null, '', '/');
  vi.mocked(getSystemModels).mockResolvedValue(structuredClone(slots()) as any);
});

const show = async (over: Record<string, unknown> = {}) => {
  vi.mocked(getSystemModels).mockResolvedValue(structuredClone(slots(over)) as any);
  render(<VisionChannelBlock />);
  await screen.findByRole('link', { name: '去渠道页改读图槽位' });
};

describe('看图渠道回显', () => {
  it('没配过就说清楚整条跟随主渠道', async () => {
    await show();
    expect(screen.getAllByText('跟随主渠道').length).toBeGreaterThan(0);
  });

  it('显示的是变量引用原文，同时给出解析后的值', async () => {
    await show({ model: 'gemini-3.5-flash', model_raw: '${VISION_MODEL}', provider: 'gemini' });
    expect(screen.getByText('${VISION_MODEL}')).toBeTruthy();
    expect(screen.getByText('解析为 gemini-3.5-flash')).toBeTruthy();
    expect(screen.getByText('gemini')).toBeTruthy();
  });

  it('填了地址却没选渠道时点明按 OpenAI 格式发', async () => {
    await show({ base_url_raw: 'https://relay.example/v1', base_url: 'https://relay.example/v1' });
    expect(screen.getByText('openai（没选渠道，按 OpenAI 格式发）')).toBeTruthy();
  });

  it('密钥只报有没有，不回显', async () => {
    await show({ api_key_present: true, api_key_redacted: '****abcd' });
    expect(screen.getByText('****abcd')).toBeTruthy();
  });

  it('这一块不写配置，页面上没有任何保存入口', async () => {
    await show();
    expect(screen.queryByRole('button', { name: /保存/ })).toBeNull();
    expect(updateSystemModel).not.toHaveBeenCalled();
  });
});

describe('去渠道页', () => {
  it('链接切到渠道页并把锚点留在地址里', async () => {
    await show();
    fireEvent.click(screen.getByRole('link', { name: '去渠道页改读图槽位' }));

    expect(useViewStore.getState().activeSettingsTab).toBe('qq-providers');
    expect(window.location.hash).toBe(`#${SYSTEM_SLOTS_ANCHOR}`);
  });

  it('是真链接，能中键开新页', async () => {
    await show();
    const link = screen.getByRole('link', { name: '去渠道页改读图槽位' }) as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(`?view=settings&tab=qq-providers#${SYSTEM_SLOTS_ANCHOR}`);
  });
});
