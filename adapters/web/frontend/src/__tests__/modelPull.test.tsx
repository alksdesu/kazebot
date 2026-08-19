// 从上游拉模型名。密钥不在前端明文保存，所以拉取用的是「页面上填了什么就送什么，
// 没填就让后端拿存好的那份」—— 这条约定错了会变成拿旧密钥问新地址。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { listUpstreamModels } from '../api/supervisorClient';
import { ModelField } from '../console/channelFields';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  listUpstreamModels: vi.fn(),
}));

const pull = () => fireEvent.click(screen.getByRole('button', { name: '拉取' }));

const mount = (over: Partial<Parameters<typeof ModelField>[0]> = {}) => render(
  <ModelField
    apiKey=""
    ariaLabel="渠道 模型"
    baseUrl="https://relay.test/v1"
    choices={['from-elsewhere']}
    listId="lst"
    provider="openai"
    value=""
    onChange={() => {}}
    {...over}
  />,
);

describe('拉取模型名', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useSettingsStore.setState({ adminToken: 'tk' });
    vi.mocked(listUpstreamModels).mockResolvedValue(['gpt-4o', 'o3']);
  });

  it('把页面上当前的地址送过去，而不是存盘的那份', async () => {
    mount({ baseUrl: 'https://just-typed.test/v1' });
    pull();

    await waitFor(() => expect(listUpstreamModels).toHaveBeenCalledWith(
      'tk', 'openai', { base_url: 'https://just-typed.test/v1' },
    ));
  });

  it('密钥没改就不送，让后端用存好的', async () => {
    mount({ apiKey: '   ' });
    pull();

    await waitFor(() => expect(vi.mocked(listUpstreamModels).mock.calls[0][2])
      .not.toHaveProperty('api_key'));
  });

  it('刚敲进去的密钥要送过去，否则试的是旧钥匙', async () => {
    mount({ apiKey: 'sk-new' });
    pull();

    await waitFor(() => expect(vi.mocked(listUpstreamModels).mock.calls[0][2])
      .toMatchObject({ api_key: 'sk-new' }));
  });

  it('拉到的进候选，别处用过的也留着', async () => {
    const { container } = mount();
    pull();

    await waitFor(() => {
      const values = Array.from(container.querySelectorAll('datalist option'))
        .map((node) => node.getAttribute('value'));
      expect(values).toEqual(['gpt-4o', 'o3', 'from-elsewhere']);
    });
  });

  it('拉失败要把原因说出来，不能只是没反应', async () => {
    vi.mocked(listUpstreamModels).mockRejectedValue(new Error('502 上游返回 401'));
    mount();
    pull();

    await waitFor(() => expect(screen.getByText(/502 上游返回 401/)).toBeTruthy());
  });

  it('没有令牌时按钮点不动', () => {
    useSettingsStore.setState({ adminToken: null });
    mount();

    expect(screen.getByRole('button', { name: '拉取' })).toHaveProperty('disabled', true);
  });
});
