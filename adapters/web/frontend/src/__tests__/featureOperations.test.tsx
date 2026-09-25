import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { DiagnosticsPanel } from '../features/operations/DiagnosticsPanel';
import { useViewStore } from '../store/viewStore';
import { WorkspaceNav } from '../features/WorkspaceNav';

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

describe('feature workspaces and operations', () => {
  it('keeps workspace URLs separate from settings tabs', () => {
    render(<WorkspaceNav />);
    fireEvent.click(screen.getByRole('button', { name: '资料' }));
    expect(useViewStore.getState().viewMode).toBe('materials');
    expect(window.location.search).toContain('view=materials');
    expect(window.location.search).not.toContain('tab=');
    useViewStore.getState().setSettingsTab('qq-account');
    expect(useViewStore.getState().viewMode).toBe('settings');
  });

  it('does not probe the paid model unless selected', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ id: 'test', created_at: new Date().toISOString(), status: 'passed', checks: [] }));
    vi.stubGlobal('fetch', fetchMock);
    render(<DiagnosticsPanel />);
    fireEvent.click(screen.getByRole('button', { name: '开始诊断' }));
    await screen.findByRole('button', { name: '导出报告' });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ test_model: false, include_logs: false });
  });
});
