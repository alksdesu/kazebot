// 渠道编辑不能吃掉 ${VAR} 这层间接：回填展开值再保存一次，配置里的引用就没了。
// config.example.yaml:48 推荐的正是 model: "${OPENAI_MODEL}" 这种写法。
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

const ENV_PROVIDERS = {
  active_provider: 'openai',
  providers: {
    openai: {
      model: 'gpt-4o-mini',
      model_raw: '${OPENAI_MODEL}',
      base_url: 'https://api.openai.com/v1',
      base_url_raw: '${OPENAI_BASE_URL}',
      api_key_present: true,
      api_key_redacted: '****cdef',
    },
    unset: {
      model: '',
      model_raw: '${MISSING_MODEL}',
      base_url: '',
      base_url_raw: '',
      api_key_present: false,
      api_key_redacted: '',
    },
  },
  fallbacks: [],
  registered: ['openai', 'unset'],
};

const boxesFor = (container: HTMLElement, suffix: string) =>
  [...container.querySelectorAll('input')]
    .filter((input) => (input.getAttribute('aria-label') || '').endsWith(suffix));

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  vi.mocked(getNodes).mockResolvedValue([] as any);
  vi.mocked(getProviders).mockResolvedValue(ENV_PROVIDERS as any);
  vi.mocked(upsertProvider).mockResolvedValue(ENV_PROVIDERS as any);
});

describe('渠道编辑器与环境变量引用', () => {
  it('输入框回填的是展开前的原文', async () => {
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(boxesFor(container, '模型')).toHaveLength(2));

    expect(boxesFor(container, '模型')[0]).toHaveValue('${OPENAI_MODEL}');
    expect(boxesFor(container, '地址')[0]).toHaveValue('${OPENAI_BASE_URL}');
  });

  it('只改密钥再保存，没动过的字段提交回去还是那份原文', async () => {
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(boxesFor(container, '模型')).toHaveLength(2));
    fireEvent.change(boxesFor(container, '密钥')[0], { target: { value: 'sk-new' } });
    fireEvent.click(screen.getByRole('button', { name: '保存渠道' }));

    await waitFor(() => expect(upsertProvider).toHaveBeenCalled());
    expect(vi.mocked(upsertProvider).mock.calls[0][2]).toMatchObject({
      model: '${OPENAI_MODEL}',
      base_url: '${OPENAI_BASE_URL}',
    });
  });

  it('旁边交代它当前指向谁', async () => {
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(boxesFor(container, '模型')).toHaveLength(2));

    expect(screen.getByText('解析为 gpt-4o-mini')).toBeInTheDocument();
    expect(screen.getByText('解析为 https://api.openai.com/v1')).toBeInTheDocument();
  });

  it('变量没设时说清楚是空的，不是装作配好了', async () => {
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(boxesFor(container, '模型')).toHaveLength(2));

    expect(screen.getByText('环境变量未设置，解析为空')).toBeInTheDocument();
  });

  it('值是字面量时不多嘴', async () => {
    vi.mocked(getProviders).mockResolvedValue({
      ...ENV_PROVIDERS,
      providers: {
        openai: {
          model: 'gpt-4o-mini', model_raw: 'gpt-4o-mini',
          base_url: 'https://api.openai.com/v1', base_url_raw: 'https://api.openai.com/v1',
          api_key_present: true, api_key_redacted: '****cdef',
        },
      },
    } as any);
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(boxesFor(container, '模型')).toHaveLength(1));

    expect(screen.queryByText(/^解析为/)).toBeNull();
  });

  it('改成别的变量名后不再显示上一份解析结果', async () => {
    // 展开值来自服务端上次返回的那份原文，配不上刚敲进去的名字。
    const { container } = render(<ProvidersPage />);

    await waitFor(() => expect(boxesFor(container, '模型')).toHaveLength(2));
    expect(screen.getByText('解析为 gpt-4o-mini')).toBeInTheDocument();

    fireEvent.change(boxesFor(container, '模型')[0], { target: { value: '${OTHER_MODEL}' } });

    expect(screen.queryByText('解析为 gpt-4o-mini')).toBeNull();
  });
});
