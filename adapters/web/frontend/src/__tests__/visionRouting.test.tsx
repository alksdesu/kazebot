// 带图消息的路由档位。写的是 runtime.yaml 原文，所以既要认得出别人写的各种布尔写法，
// 也不能在回写时把这份文件的其它内容搅乱。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import yaml from 'js-yaml';

import {
  ApiError, getRuntimeFile, getRuntimeLayers, ReceiptUnreadable, updateRuntimeRaw, type RuntimeFile,
} from '../api/supervisorClient';
import { VisionRouting, normalizeMode } from '../console/VisionRouting';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getRuntimeFile: vi.fn(),
  getRuntimeLayers: vi.fn(),
  updateRuntimeRaw: vi.fn(),
}));

const RUNTIME = [
  'shell:',
  '  entry_node_id: qq.orchestrator',
  'routing:',
  '  vision:',
  '    enabled: auto',
  '    entry_node_id: qq.vision',
  '',
].join('\n');

// 打开时读两层（生效值不另给就是原文里写的），保存前现读原文：两边都换成这一版
const setRuntime = (
  text: string, hash: string | null = 'runtime-1', source: RuntimeFile['source'] = 'override',
  effective = (yaml.load(text) ?? {}) as Record<string, unknown>,
) => {
  const file = { content: text, hash, source, path: source === 'override' ? 'config/runtime.yaml' : 'defaults/runtime.yaml' } as RuntimeFile;
  vi.mocked(getRuntimeFile).mockResolvedValue(file);
  vi.mocked(getRuntimeLayers).mockResolvedValue({ ...file, defaults: {}, effective });
};

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token' });
  setRuntime(RUNTIME);
  vi.mocked(updateRuntimeRaw).mockResolvedValue({ ok: true, hash: 'runtime-2' });
});

const picker = () => screen.getByLabelText('带图消息路由') as HTMLSelectElement;
const ready = () => screen.findByText('带图消息');

describe('归一化', () => {
  it.each(['true', 'True', '1', 'yes', 'on', '"true"'])('%s 归到总是绕', (raw) => {
    expect(normalizeMode(raw)).toBe('true');
  });

  it.each(['false', '0', 'no', 'off'])('%s 归到从不绕', (raw) => {
    expect(normalizeMode(raw)).toBe('false');
  });

  it.each(['auto', '', '  ', 'whatever'])('%s 归到自动', (raw) => {
    // 认不出的值当 auto：那是最不容易出错的一档。
    expect(normalizeMode(raw)).toBe('auto');
  });
});

describe('读取', () => {
  it('读取失败时不允许用空配置覆盖 runtime.yaml', async () => {
    vi.mocked(getRuntimeLayers).mockRejectedValue(new Error('synthetic offline'));
    render(<VisionRouting />);
    expect(await screen.findByText('synthetic offline')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存路由' })).not.toBeInTheDocument();
    expect(updateRuntimeRaw).not.toHaveBeenCalled();
  });
  it('读出当前档位', async () => {
    render(<VisionRouting />);
    await ready();
    expect(picker().value).toBe('auto');
  });

  it('老配置写的 true 认得出来', async () => {
    setRuntime(RUNTIME.replace('enabled: auto', 'enabled: true'));
    render(<VisionRouting />);
    await ready();
    expect(picker().value).toBe('true');
  });

  it('哪一层都没写过这一项就是自动', async () => {
    setRuntime('shell:\n  entry_node_id: qq.orchestrator\n');
    render(<VisionRouting />);
    await ready();
    expect(picker().value).toBe('auto');
  });

  // 本实例那份没写这一项时，生效的是默认那层的档位，不能显示成页面自己猜的「自动」
  it('本实例没写过这一项时显示默认那层实际给的档位，并说明是默认的', async () => {
    setRuntime('shell:\n  entry_node_id: qq.orchestrator\n', 'runtime-1', 'override', {
      shell: { entry_node_id: 'qq.orchestrator' }, routing: { vision: { enabled: false } },
    });
    render(<VisionRouting />);
    await ready();
    expect(picker().value).toBe('false');
    expect(screen.getByText(/本实例没设过，用的是默认的这一档/)).toBeInTheDocument();

    fireEvent.change(picker(), { target: { value: 'true' } });
    expect(screen.queryByText(/本实例没设过/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '保存路由' }));
    await waitFor(() => expect(updateRuntimeRaw).toHaveBeenCalled());
    fireEvent.change(picker(), { target: { value: 'false' } });
    expect(screen.queryByText(/本实例没设过/)).toBeNull();
  });

  it('本实例自己写了这一项就不说是默认的', async () => {
    render(<VisionRouting />);
    await ready();
    expect(screen.queryByText(/本实例没设过/)).toBeNull();
  });

  it('没动过就存不了', async () => {
    render(<VisionRouting />);
    await ready();
    expect((screen.getByRole('button', { name: '保存路由' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('保存', () => {
  it('只改那一个键，文件其余部分原样留着', async () => {
    render(<VisionRouting />);
    await ready();
    fireEvent.change(picker(), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: '保存路由' }));

    await waitFor(() => expect(updateRuntimeRaw).toHaveBeenCalled());
    const written = vi.mocked(updateRuntimeRaw).mock.calls[0][1];
    expect(written).toContain('enabled: false');
    expect(written).toContain('entry_node_id: qq.vision');
    expect(written).toContain('entry_node_id: qq.orchestrator');
  });

  it('这一项没写过时也能补进去', async () => {
    setRuntime('shell:\n  entry_node_id: qq.orchestrator\n');
    render(<VisionRouting />);
    await ready();
    fireEvent.change(picker(), { target: { value: 'true' } });
    fireEvent.click(screen.getByRole('button', { name: '保存路由' }));

    await waitFor(() => expect(updateRuntimeRaw).toHaveBeenCalled());
    const written = vi.mocked(updateRuntimeRaw).mock.calls[0][1];
    expect(written).toMatch(/routing:[\s\S]*vision:[\s\S]*enabled: true/);
    expect(written).toContain('entry_node_id: qq.orchestrator');
  });

  // 读到的是仓库默认时，写回从空的本实例层开始：默认全文抄过去，之后仓库默认再改就盖不过来了。
  it('本实例还没有自己的 runtime.yaml 时只落这一个键', async () => {
    setRuntime(RUNTIME, 'defaults-1', 'default');
    render(<VisionRouting />);
    await ready();
    fireEvent.change(picker(), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: '保存路由' }));

    await waitFor(() => expect(updateRuntimeRaw).toHaveBeenCalled());
    expect(vi.mocked(updateRuntimeRaw).mock.calls[0]).toEqual(['admin-token', 'routing:\n  vision:\n    enabled: false\n', 'defaults-1']);
  });

  it('保存通过但带着提醒时，提醒接在结果后面', async () => {
    vi.mocked(updateRuntimeRaw).mockResolvedValue({ ok: true, hash: 'runtime-2', warnings: ['routing.vision.entry_node_id 指向的节点不存在'] });
    render(<VisionRouting />);
    await ready();
    fireEvent.change(picker(), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: '保存路由' }));

    expect(await screen.findByText('已保存。下一条带图消息就按新规矩走。routing.vision.entry_node_id 指向的节点不存在')).toBeTruthy();
  });

  // 写已经落盘了：说成失败，档位停在没存的样子，人会再存一次。
  it('回执没读懂时照存上了说，回执的事另起一行', async () => {
    vi.mocked(updateRuntimeRaw).mockRejectedValue(new ReceiptUnreadable('runtime.yaml 已经存上，可回执格式不对：缺 hash'));
    render(<VisionRouting />);
    await ready();
    fireEvent.change(picker(), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: '保存路由' }));

    expect(await screen.findByText('已保存。下一条带图消息就按新规矩走。')).toBeTruthy();
    expect(screen.getByText('runtime.yaml 已经存上，可回执格式不对：缺 hash')).toBeTruthy();
    expect(screen.getByRole('button', { name: '保存路由' })).toBeDisabled();
  });

  it('写失败时说出来，不假装存上了', async () => {
    vi.mocked(updateRuntimeRaw).mockRejectedValue(new ApiError(403, 'forbidden'));
    render(<VisionRouting />);
    await ready();
    fireEvent.change(picker(), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: '保存路由' }));

    expect(await screen.findByText(/403 forbidden/)).toBeTruthy();
    expect(picker().value).toBe('false');
  });
});

describe('说明文字', () => {
  it('自动档才提视觉节点的环境变量', async () => {
    // 只有这一档才可能真的绕过去，其它两档说它没意义。
    render(<VisionRouting />);
    await ready();
    expect(screen.getByText(/CLONOTH_VISION_MODEL/)).toBeTruthy();

    fireEvent.change(picker(), { target: { value: 'false' } });
    expect(screen.queryByText(/CLONOTH_VISION_MODEL/)).toBeNull();
  });

  it('每一档都说清楚会发生什么', async () => {
    render(<VisionRouting />);
    await ready();
    expect(screen.getByText(/收得下图就直接交给它/)).toBeTruthy();

    fireEvent.change(picker(), { target: { value: 'true' } });
    expect(screen.getByText(/那里没有任何工具/)).toBeTruthy();
  });
});
