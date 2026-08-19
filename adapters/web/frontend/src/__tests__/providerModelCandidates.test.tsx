// 渠道页的模型字段。候选只来自同一份 providers 响应，所以不存在独立的降级路径；
// 真正要盯住的是「不摆自己那份」和「输入框仍收任意字符串」。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getNodes, getProviders, upsertProvider } from '../api/supervisorClient';
import { ProvidersPage } from '../console/ProvidersPage';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getNodes: vi.fn(),
  getProviders: vi.fn(),
  upsertProvider: vi.fn(),
}));

// model_raw / base_url_raw 是后端另给的展开前原文，编辑框回填的是它。
const block = (model: string, raw = model, base_url = '', base_url_raw = base_url) =>
  ({ base_url, model, base_url_raw, model_raw: raw, api_key_present: false, api_key_redacted: '' });

const PROVIDERS = {
  active_provider: 'openai',
  providers: {
    openai: block('gpt-4o-mini'),
    deepseek: block('deepseek-v4-pro'),
  },
  fallbacks: [{ ...block('claude-sonnet-4-6'), provider: 'openai' }],
  registered: ['openai', 'deepseek'],
};

const LONE_PROVIDER = {
  active_provider: 'openai',
  providers: { openai: block('gpt-4o-mini') },
  fallbacks: [],
  registered: ['openai'],
};

const datalists = (container: HTMLElement) => [...container.querySelectorAll('datalist')];

const optionValues = (list: HTMLDataListElement) =>
  [...list.querySelectorAll('option')].map((node) => node.value);

const modelBoxes = (container: HTMLElement) =>
  [...container.querySelectorAll('input')]
    .filter((input) => (input.getAttribute('aria-label') || '').endsWith('模型'));

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  vi.mocked(getNodes).mockResolvedValue([] as any);
  vi.mocked(getProviders).mockResolvedValue(PROVIDERS as any);
  vi.mocked(upsertProvider).mockResolvedValue(PROVIDERS as any);
});

describe('渠道模型候选', () => {
  it('每张卡摆的是别的渠道和备用链在用的值，不摆自己那份', async () => {
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(datalists(container)).toHaveLength(2));

    // 卡片按渠道名排序：deepseek 在前，openai 在后。
    // 活跃渠道排候选最前，本卡自己的 deepseek-v4-pro 不在里面。
    expect(optionValues(datalists(container)[0])).toEqual(['gpt-4o-mini', 'claude-sonnet-4-6']);
    expect(optionValues(datalists(container)[1])).toEqual(['deepseek-v4-pro', 'claude-sonnet-4-6']);

    const boxes = modelBoxes(container);
    expect(boxes[0].getAttribute('list')).toBe(datalists(container)[0].id);
    expect(boxes[1].getAttribute('list')).toBe(datalists(container)[1].id);
  });

  it('备用链里重复写了本渠道的值，同样不摆进这张卡', async () => {
    // 排除按值做而不是按渠道名：按名只剔得掉 providers 那一份，备用链里那条同值的会绕回来。
    vi.mocked(getProviders).mockResolvedValue({
      ...PROVIDERS,
      fallbacks: [{ provider: 'openai', model: 'gpt-4o-mini' }, { provider: 'x', model: 'claude-sonnet-4-6' }],
    } as any);
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(datalists(container)).toHaveLength(2));

    expect(optionValues(datalists(container)[1])).not.toContain('gpt-4o-mini');
    expect(optionValues(datalists(container)[0])).toContain('gpt-4o-mini');
  });

  it('只有一个渠道又没有备用链时退成纯输入框', async () => {
    vi.mocked(getProviders).mockResolvedValue(LONE_PROVIDER as any);
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(modelBoxes(container)).toHaveLength(1));

    expect(datalists(container)).toHaveLength(0);
    expect(modelBoxes(container)[0]).not.toHaveAttribute('list');
    expect(modelBoxes(container)[0]).toHaveValue('gpt-4o-mini');
  });

  it('收下不在候选里的值并原样提交', async () => {
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(modelBoxes(container)).toHaveLength(2));

    fireEvent.change(modelBoxes(container)[0], { target: { value: 'relay/qwen3-max' } });
    expect(modelBoxes(container)[0]).toHaveValue('relay/qwen3-max');

    fireEvent.click(screen.getByRole('button', { name: '保存渠道' }));

    await waitFor(() => expect(upsertProvider).toHaveBeenCalled());
    expect(vi.mocked(upsertProvider).mock.calls[0][2]).toMatchObject({ model: 'relay/qwen3-max' });
  });

  it('渠道列表拉不到时整页照常报错，不因为候选而崩', async () => {
    vi.mocked(getProviders).mockRejectedValue(new Error('403'));
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(screen.getByText('403')).toBeInTheDocument());

    expect(datalists(container)).toHaveLength(0);
  });
});
