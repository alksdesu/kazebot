// 带图消息的路由档位。写的是 runtime.yaml 原文，所以既要认得出别人写的各种布尔写法，
// 也不能在回写时把这份文件的其它内容搅乱。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getRuntimeRaw, updateRuntimeRaw } from '../api/supervisorClient';
import { VisionRouting, normalizeMode } from '../console/VisionRouting';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getRuntimeRaw: vi.fn(),
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

const setRuntime = (text: string) => vi.mocked(getRuntimeRaw).mockResolvedValue(text);

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token' });
  setRuntime(RUNTIME);
  vi.mocked(updateRuntimeRaw).mockResolvedValue({} as any);
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

  it('没写过这一项就是自动', async () => {
    setRuntime('shell:\n  entry_node_id: qq.orchestrator\n');
    render(<VisionRouting />);
    await ready();
    expect(picker().value).toBe('auto');
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

  it('写失败时说出来，不假装存上了', async () => {
    vi.mocked(updateRuntimeRaw).mockRejectedValue(new Error('403 forbidden'));
    render(<VisionRouting />);
    await ready();
    fireEvent.change(picker(), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: '保存路由' }));

    expect(await screen.findByText(/403 forbidden/)).toBeTruthy();
    expect(picker().value).toBe('false');
  });
});

describe('说明文字', () => {
  it('自动档才提 QQ_VISION 变量', async () => {
    // 只有这一档才可能真的绕过去，其它两档说它没意义。
    render(<VisionRouting />);
    await ready();
    expect(screen.getByText(/QQ_VISION_MODEL/)).toBeTruthy();

    fireEvent.change(picker(), { target: { value: 'false' } });
    expect(screen.queryByText(/QQ_VISION_MODEL/)).toBeNull();
  });

  it('每一档都说清楚会发生什么', async () => {
    render(<VisionRouting />);
    await ready();
    expect(screen.getByText(/收得下图就直接交给它/)).toBeTruthy();

    fireEvent.change(picker(), { target: { value: 'true' } });
    expect(screen.getByText(/那里没有任何工具/)).toBeTruthy();
  });
});
