import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DiagnosticsPanel } from './DiagnosticsPanel';

const json = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());

describe('operations forms', () => {
  it('freezes diagnostic options during a run and labels the older report on failure', async () => {
    let fail!: (reason: Error) => void;
    const fetchMock = vi.fn().mockResolvedValueOnce(json({ id: 'first', created_at: '2030-01-01T00:00:00Z', status: 'passed', checks: [] })).mockImplementation(() => new Promise((_resolve, reject) => { fail = reject; }));
    vi.stubGlobal('fetch', fetchMock); render(<DiagnosticsPanel />);
    fireEvent.click(screen.getByRole('button', { name: '开始诊断' })); await screen.findByRole('button', { name: '导出报告' });
    fireEvent.click(screen.getByRole('button', { name: '开始诊断' }));
    expect(screen.getByRole('checkbox', { name: /最小模型请求/ })).toBeDisabled();
    await act(async () => fail(Error('connection failed')));
    await waitFor(() => expect(screen.getByText(/保留的是上一份诊断报告/)).toBeInTheDocument());
    expect(screen.getByRole('button', { name: '导出报告' })).toBeEnabled();
  });

  // 检查已经跑过了：报成失败的话，人会再点一次，勾了模型请求的还要再计一次费。
  it('a report it cannot read says the checks ran', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ id: 'second', created_at: '2030-01-01T00:00:00Z', status: 'passed' })));
    render(<DiagnosticsPanel />);
    fireEvent.click(screen.getByRole('button', { name: '开始诊断' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('诊断已经做完，可报告格式不对：缺 checks。这次的结果显示不了，要看结果只能再诊断一次');
    expect(screen.queryByRole('button', { name: '导出报告' })).toBeNull();
  });
});
