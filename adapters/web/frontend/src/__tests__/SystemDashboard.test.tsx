// [2026-06-01] System dashboard tests for the chat right rail.
// Why: the main chat right panel should show system status instead of session editing.
// How: mock Supervisor state and health responses, then assert the displayed counters.
// Purpose: the right rail remains a stable operations dashboard across chat turns.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SystemDashboard } from '../components/dashboard/SystemDashboard';
import { useChatStore } from '../store/chatStore';
import { useSettingsStore } from '../store/settingsStore';

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('SystemDashboard', () => {
  beforeEach(() => {
    useSettingsStore.setState({ adminToken: 'admin-token', isConnected: true });
    // [AutoC 2026-06-04] Why: ActiveTasksModal now reads transient task activity
    // from chatStore. How: reset that map with the connection state in each test.
    // Purpose: live status from one modal test cannot leak into another.
    useChatStore.setState({ connectionStatus: 'open', taskActivities: {} });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/v1/admin/state')) {
        return jsonResponse({
          sessions: 3,
          approvals: { pending: 2, allowed: 1, denied: 0 },
          tasks: { pending: 1, running: 4, suspended: 1, completed: 10, failed: 0, cancelled: 0 },
          engine_runtime: { worker_id: 'worker-a', last_seen_at: '2026-06-01T14:00:00.000Z', workers: ['worker-a'] },
        });
      }
      if (url.endsWith('/v1/health')) {
        return jsonResponse({ status: 'ok', run_id: 'run-1', workspace_root: '/repo', started_at: '2026-06-01T13:00:00.000Z', uptime_seconds: 3723 });
      }
      if (url.endsWith('/v1/admin/tasks/active')) {
        // [AutoC 2026-06-04] Why: clicking the task-count card should open a live
        // detail modal. How: provide one active task summary from the new endpoint.
        // Purpose: the dashboard test covers the modal integration without using
        // the chat stream or WebSocket events.
        const now = Date.now();
        return jsonResponse([{
          task_id: 'task-alpha-123456',
          session_id: 'session-alpha',
          node_id: 'node-alpha',
          status: 'running',
          kind: 'node',
          created_at: new Date(now - 65_000).toISOString(),
          updated_at: new Date(now - 3_000).toISOString(),
          worker_id: 'wk-123456789',
          caller_task_id: null,
          input_summary: 'Inspect current production state',
          cancel_requested: false,
        }]);
      }
      return jsonResponse({});
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders admin state, health, worker, and connection status', async () => {
    render(<SystemDashboard />);

    await waitFor(() => expect(screen.getByText('3')).toBeInTheDocument());

    expect(screen.getByText('系统仪表盘')).toBeInTheDocument();
    expect(screen.getByText('会话数')).toBeInTheDocument();
    expect(screen.getByText('待审批')).toBeInTheDocument();
    expect(screen.getByText('运行中任务')).toBeInTheDocument();
    expect(screen.getByText('1小时 2分钟')).toBeInTheDocument();
    expect(screen.getByText('worker-a')).toBeInTheDocument();
    expect(screen.getByText('WebSocket 已连接')).toBeInTheDocument();
  });

  it('opens the active task modal from the running task statistic', async () => {
    render(<SystemDashboard />);

    await waitFor(() => expect(screen.getByText('6')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '查看运行中任务详情' }));

    await waitFor(() => expect(screen.getByRole('dialog', { name: '活跃任务详情' })).toBeInTheDocument());
    expect(screen.getByText('node-alpha')).toBeInTheDocument();
    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.getByText('Inspect current production state')).toBeInTheDocument();
    expect(screen.getByText('wk-12345')).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith('/v1/admin/tasks/active', {
      headers: { Authorization: 'Bearer admin-token' },
    });
  });

  it('shows live task activity and calls the single-task cancel API', async () => {
    // [AutoC 2026-06-04] Why: the modal should prefer transient WebSocket activity
    // over the coarser polled task status. How: seed chatStore with a live tool-call
    // status before opening the modal. Purpose: the row shows the operator what the
    // task is doing right now while keeping the polling fallback.
    useChatStore.setState({
      taskActivities: {
        'task-alpha-123456': { phase: 'tool_call', detail: 'read_file', lastEventAt: Date.now() },
      },
    });

    render(<SystemDashboard />);

    await waitFor(() => expect(screen.getByText('6')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '查看运行中任务详情' }));

    await waitFor(() => expect(screen.getByText('工具: read_file')).toBeInTheDocument());
    expect(screen.queryByText('running')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '取消任务 task-alpha-123456' }));

    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/v1/tasks/task-alpha-123456/cancel', {
      method: 'POST',
      headers: { Authorization: 'Bearer admin-token', 'X-Admin-Token': 'admin-token' },
    }));
  });
});
