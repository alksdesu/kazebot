// 模型参数页。整页照后端公布的清单渲染，所以这里盯的是「清单怎么说，界面就怎么变」，
// 以及两层作用域各自写进了哪个文件 —— 写错文件的话改动会静静地不生效。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import yaml from 'js-yaml';

import {
  getNodeRaw,
  getNodes,
  getProviderProfiles,
  getProviders,
  getRuntimeRaw,
  updateNodeRaw,
  updateRuntimeRaw,
} from '../api/supervisorClient';
import { SettingsPageHost } from '../components/settings/SettingsPageHost';
import { SettingsSidebar } from '../components/settings/SettingsSidebar';
import { useConsoleStore } from '../console/consoleStore';
import { ModelsPage } from '../console/ModelsPage';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getNodeRaw: vi.fn(),
  getNodes: vi.fn(),
  getProviderProfiles: vi.fn(),
  getProviders: vi.fn(),
  getRuntimeRaw: vi.fn(),
  updateNodeRaw: vi.fn(),
  updateRuntimeRaw: vi.fn(),
}));

const RUNTIME = [
  '# 别动我',
  'engine:',
  '  tool_mode: json',
  '',
  'providers:',
  '  openai:',
  '    timeout_sec: 600.0',
  '',
].join('\n');

const NODE_YAML = ['id: qq.vision', 'type: ai', 'provider: gemini', 'model: x', ''].join('\n');

const CATALOG = {
  openai: [
    {
      key: 'reasoning_effort',
      label: '思考档位',
      kind: 'enum',
      default: 'default',
      choices: [
        { value: 'default', label: '不指定' },
        { value: 'high', label: '高' },
      ],
    },
    { key: 'temperature', label: '随机度', kind: 'float', default: null, minimum: 0, maximum: 2 },
  ],
  gemini: [
    {
      key: 'thinking_mode',
      label: '思考模式',
      kind: 'enum',
      default: 'auto',
      choices: [
        { value: 'auto', label: '跟随模型默认' },
        { value: 'budget', label: '按预算' },
      ],
    },
    {
      key: 'thinking_budget',
      label: '思考预算',
      kind: 'int',
      default: 8192,
      minimum: 0,
      maximum: 32768,
      depends_on: { key: 'thinking_mode', value: 'budget' },
    },
  ],
};

const PROVIDERS = { active_provider: 'openai', providers: {}, fallbacks: [], registered: ['openai'] };
const NODES = [{ id: 'qq.vision', type: 'ai', provider: 'gemini' }];

beforeEach(() => {
  // 断言里有「不该调用另一个端点」，调用记录不清会串到下一个用例。
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token' });
  vi.mocked(getProviderProfiles).mockResolvedValue({ options: CATALOG, wireFormats: {}, hostProfiles: {} } as any);
  vi.mocked(getProviders).mockResolvedValue(PROVIDERS as any);
  vi.mocked(getNodes).mockResolvedValue(NODES as any);
  vi.mocked(getRuntimeRaw).mockResolvedValue(RUNTIME);
  vi.mocked(getNodeRaw).mockResolvedValue(NODE_YAML);
  vi.mocked(updateRuntimeRaw).mockResolvedValue({ ok: true } as any);
  vi.mocked(updateNodeRaw).mockResolvedValue({ ok: true } as any);
});

const save = () => fireEvent.click(screen.getByRole('button', { name: '保存参数' }));

describe('模型参数页', () => {
  it('照清单渲染控件，不自己写死参数名', async () => {
    render(<ModelsPage />);
    expect(await screen.findByText('思考档位')).toBeTruthy();
    expect(screen.getByText('随机度')).toBeTruthy();
    // 别家 provider 的参数不该出现在当前渠道下。
    expect(screen.queryByText('思考模式')).toBeNull();
  });

  it('全局作用域写 runtime.yaml，且保住原有注释', async () => {
    render(<ModelsPage />);
    fireEvent.click(await screen.findByRole('button', { name: '高' }));
    save();
    await waitFor(() => expect(updateRuntimeRaw).toHaveBeenCalled());
    const written = vi.mocked(updateRuntimeRaw).mock.calls[0][1];
    expect(written).toContain('# 别动我');
    expect(yaml.load(written)).toMatchObject({
      providers: { openai: { timeout_sec: 600, options: { reasoning_effort: 'high' } } },
    });
    expect(updateNodeRaw).not.toHaveBeenCalled();
  });

  it('切到节点后写的是节点文件，参数跟着节点声明的渠道走', async () => {
    render(<ModelsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'qq.vision' }));
    // 节点写了 provider: gemini，界面就该换成 gemini 的清单。
    expect(await screen.findByText('思考模式')).toBeTruthy();
    expect(screen.queryByText('思考档位')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '按预算' }));
    save();
    await waitFor(() => expect(updateNodeRaw).toHaveBeenCalled());
    const [, nodeId, written] = vi.mocked(updateNodeRaw).mock.calls[0];
    expect(nodeId).toBe('qq.vision');
    expect(yaml.load(written)).toMatchObject({
      id: 'qq.vision',
      provider_options: { thinking_mode: 'budget' },
    });
    expect(updateRuntimeRaw).not.toHaveBeenCalled();
  });

  it('依赖项没满足时子控件不出现', async () => {
    render(<ModelsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'qq.vision' }));
    // thinking_mode 默认 auto，预算这一项此时无意义。
    expect(await screen.findByText('思考模式')).toBeTruthy();
    expect(screen.queryByText('思考预算')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '按预算' }));
    expect(screen.getByText('思考预算')).toBeTruthy();
  });

  it('没改动时保存按钮是灰的', async () => {
    render(<ModelsPage />);
    await screen.findByText('思考档位');
    expect((screen.getByRole('button', { name: '保存参数' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('换作用域会丢掉上一份草稿，不把值带到别的文件里', async () => {
    render(<ModelsPage />);
    fireEvent.click(await screen.findByRole('button', { name: '高' }));
    fireEvent.click(screen.getByRole('button', { name: 'qq.vision' }));
    await screen.findByText('思考模式');
    fireEvent.click(screen.getByRole('button', { name: '全局' }));
    await screen.findByText('思考档位');
    expect((screen.getByRole('button', { name: '保存参数' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('拿不到清单时说清楚，而不是摆一页空白', async () => {
    vi.mocked(getProviderProfiles).mockRejectedValue(new Error('401 unauthorized'));
    render(<ModelsPage />);
    expect(await screen.findByText(/401 unauthorized/)).toBeTruthy();
  });
});

describe('设置页外壳里的模型页', () => {
  beforeEach(() => {
    useConsoleStore.setState({ live: null, loading: false, draft: {} });
    useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'qq-models' });
    useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  });

  it('bot 没跑也能打开——这一页写的不是 qq.yaml', async () => {
    // live 为空时其它页显示「bot 还没公布生效配置」，模型页不该被这个门挡住。
    render(<SettingsPageHost />);
    expect(await screen.findByText('思考档位')).toBeTruthy();
    expect(screen.queryByText(/还没公布生效配置/)).toBeNull();
  });

  it('侧栏上有「模型」这一格', () => {
    render(<SettingsSidebar />);
    expect(screen.getByRole('button', { name: '模型' })).toBeTruthy();
  });
});
