// 人格页的模型字段。model 是上游 API 的字面量，前端无从校验 ——
// 候选只能当提示，输入框必须继续收任意值，否则节点里在用的 $ENV{...} 会被界面挡死。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAllToolNames,
  getNodeFileRaw,
  getNodeRaw,
  getNodes,
  getProviders,
  updateNodeRaw,
} from '../api/supervisorClient';
import { PersonaPage } from '../console/PersonaPage';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getAllToolNames: vi.fn(),
  getNodeFileRaw: vi.fn(),
  getNodeRaw: vi.fn(),
  getNodes: vi.fn(),
  getProviders: vi.fn(),
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

const modelBox = () => screen.getByPlaceholderText('留空 = 跟随全局默认模型') as HTMLInputElement;

const optionValues = (container: HTMLElement) =>
  [...container.querySelectorAll('datalist option')].map((node) => (node as HTMLOptionElement).value);

beforeEach(() => {
  useSettingsStore.setState({ adminToken: 'admin-token' });
  vi.mocked(getNodeFileRaw).mockResolvedValue('人设正文');
  vi.mocked(getNodeRaw).mockResolvedValue(ORCHESTRATOR_YAML);
  vi.mocked(getAllToolNames).mockResolvedValue(['read_file']);
  vi.mocked(getNodes).mockResolvedValue(NODES as any);
  vi.mocked(getProviders).mockResolvedValue(PROVIDERS as any);
  vi.mocked(updateNodeRaw).mockResolvedValue({ ok: true });
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

    await waitFor(() => expect(screen.getByText('read_file')).toBeInTheDocument());

    expect(container.querySelector('datalist')).toBeNull();
    expect(modelBox()).not.toHaveAttribute('list');
    expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}');
    expect(screen.getByText('人设')).toBeInTheDocument();
    expect(screen.getByText('引用原消息')).toBeInTheDocument();
  });
});

describe('模型输入', () => {
  it('收下不在候选里的值，并原样写进节点文件', async () => {
    render(<PersonaPage />);
    await waitFor(() => expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}'));

    fireEvent.change(modelBox(), { target: { value: '$ENV{MY_RELAY_MODEL}' } });
    expect(modelBox()).toHaveValue('$ENV{MY_RELAY_MODEL}');

    // 两块各自存盘，只有改过的那块的保存键是活的。
    const save = screen.getAllByRole('button', { name: '保存' }).find((button) => !(button as HTMLButtonElement).disabled);
    fireEvent.click(save!);

    await waitFor(() => expect(updateNodeRaw).toHaveBeenCalled());
    expect(vi.mocked(updateNodeRaw).mock.calls[0][2]).toContain('model: $ENV{MY_RELAY_MODEL}');
  });

  it('说清楚留空和环境变量两种写法', async () => {
    render(<PersonaPage />);
    await waitFor(() => expect(modelBox()).toHaveValue('$ENV{QQ_MAIN_MODEL}'));

    expect(modelBox()).toHaveAttribute('placeholder', '留空 = 跟随全局默认模型');
    expect(screen.getByText('$ENV{VAR}')).toBeInTheDocument();
  });
});
