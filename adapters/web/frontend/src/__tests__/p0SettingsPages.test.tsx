// [2026-06-02] P0 Settings pages regression tests.
// Why: System, Approvals, and Advanced are operator-facing tabs with polling,
// guarded actions, approval decisions, and raw YAML saves. How: render each page
// against mocked Supervisor endpoints and assert the visible Chinese UI plus API
// calls. Purpose: future settings work can refactor layout without losing the P0
// behavior requested for this batch.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AdvancedSettingsPage } from '../components/settings/pages/AdvancedSettingsPage';
import { ApprovalsSettingsPage } from '../components/settings/pages/ApprovalsSettingsPage';
import { SystemSettingsPage } from '../components/settings/pages/SystemSettingsPage';
import {
  AdvancedSettingsRightPanel,
  ApprovalsSettingsRightPanel,
  SystemSettingsRightPanel,
} from '../components/settings/panels/SettingsContextPanels';
import { settingsTabs } from '../components/settings/settingsTabs';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function textResponse(body: string): Response {
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/plain' },
  });
}

function authenticateSettings() {
  // [2026-06-02] Seed authenticated settings state for isolated page tests.
  // Why: these P0 pages intentionally hide admin controls until the General tab logs
  // in. How: set the same Zustand flags that GeneralSettingsPage writes after token
  // verification. Purpose: tests exercise page behavior without repeating login UI.
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
}

describe('P0 settings pages', () => {
  beforeEach(() => {
    localStorage.clear();
    useSettingsStore.setState({ adminToken: null, isAuthenticated: false });
    useSettingsSelectionStore.setState({ selectedApproval: null, advancedFile: 'runtime', systemLogs: [] });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('registers the three P0 tabs with the requested labels, icons, and orders', () => {
    expect(settingsTabs.find((tab) => tab.id === 'system')).toMatchObject({ label: '系统', icon: 'settings_power', order: 4, Page: SystemSettingsPage });
    expect(settingsTabs.find((tab) => tab.id === 'approvals')).toMatchObject({ label: '审批', icon: 'approval', order: 5, Page: ApprovalsSettingsPage });
    expect(settingsTabs.find((tab) => tab.id === 'advanced')).toMatchObject({ label: '高级', icon: 'code', order: 13, Page: AdvancedSettingsPage });
    expect(settingsTabs.find((tab) => tab.id === 'system')?.RightPanel).toBe(SystemSettingsRightPanel);
    expect(settingsTabs.find((tab) => tab.id === 'approvals')?.RightPanel).toBe(ApprovalsSettingsRightPanel);
    expect(settingsTabs.find((tab) => tab.id === 'advanced')?.RightPanel).toBe(AdvancedSettingsRightPanel);
  });

  it('shows the shared Admin Token login notice before authentication', () => {
    for (const Page of [SystemSettingsPage, ApprovalsSettingsPage, AdvancedSettingsPage]) {
      const view = render(<Page />);
      expect(screen.getByText('请先在通用页面登录 Admin Token')).toBeInTheDocument();
      view.unmount();
    }
  });

  it('renders System status cards and guards the engine restart action', async () => {
    authenticateSettings();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/v1/admin/state')) {
        return jsonResponse({
          sessions: 6,
          approvals: { pending: 2 },
          tasks: { running: 3, pending: 1, suspended: 1 },
          pending_approvals: [],
          engine_runtime: { worker_id: 'worker-a' },
        });
      }
      if (url.endsWith('/v1/health')) return jsonResponse({ status: 'ok', uptime_seconds: 3723 });
      if (url.endsWith('/v1/config/reload')) return jsonResponse({ ok: true, reloaded: true });
      if (url.endsWith('/v1/admin/restart')) return jsonResponse({ ok: true, target: 'engine', method: init?.method });
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SystemSettingsPage />);

    await waitFor(() => expect(screen.getByText('worker-a')).toBeInTheDocument());
    expect(screen.getByText('会话数')).toBeInTheDocument();
    expect(screen.getByText('待审批数')).toBeInTheDocument();
    expect(screen.getByText('运行中任务数')).toBeInTheDocument();
    expect(screen.getByText('1小时 2分钟')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '重载配置' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/v1/config/reload', expect.objectContaining({ method: 'POST' })));

    fireEvent.click(screen.getByRole('button', { name: '重启引擎' }));
    // 文案必须说清「只重启引擎」：以前 target=engine 实际走全量重启，连调度器一起死。
    expect(confirmSpy).toHaveBeenCalledWith(
      '确认要重启引擎吗？运行中的任务会全部中断，调度器与本页面的连接不受影响。',
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/v1/admin/restart', expect.objectContaining({ method: 'POST' })));
  });

  it('renders Approvals with inferred risk labels and sends decisions', async () => {
    authenticateSettings();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/v1/admin/state')) {
        return jsonResponse({
          sessions: 1,
          approvals: { pending: 1 },
          tasks: { running: 0 },
          pending_approvals: [{
            approval_id: 'approval-1',
            operation: '执行命令',
            node_id: 'ereuna_main',
            task_id: 'task-123',
            tool_call_id: 'tool-call-1',
            details: { tool_name: 'execute_command', path: '/tmp/demo', reason: '需要执行命令' },
          }],
          engine_runtime: {},
        });
      }
      if (url.endsWith('/v1/approvals/approval-1')) return jsonResponse({ ok: true, method: init?.method });
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ApprovalsSettingsPage />);

    await waitFor(() => expect(screen.getByText('execute_command')).toBeInTheDocument());
    expect(screen.getByText('高风险')).toBeInTheDocument();
    expect(screen.getByText('ereuna_main')).toBeInTheDocument();
    expect(screen.getByText('task-123')).toBeInTheDocument();
    expect(screen.getByText('执行命令')).toBeInTheDocument();
    expect(screen.getByText('/tmp/demo')).toBeInTheDocument();
    expect(screen.getByText('需要执行命令')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '允许' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/v1/approvals/approval-1', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ decision: 'allow', comment: 'settings allow' }),
    })));
  });

  it('keeps Advanced sections collapsed, saves runtime through a structured form, and keeps policy raw editing advanced', async () => {
    authenticateSettings();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'PUT') return jsonResponse({ ok: true });
      if (url.endsWith('/v1/admin/config/runtime/raw')) return jsonResponse({ content: 'engine:\n  tool_mode: json\n  max_workers: 4\nshell:\n  entry_node_id: ereuna_main\n' });
      if (url.endsWith('/v1/admin/config/policy/raw')) return jsonResponse({ content: 'rules: []\n' });
      if (url.endsWith('/v1/admin/config/schedules/raw')) return textResponse('schedules: []\n');
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<AdvancedSettingsPage />);

    const runtimeDetails = screen.getByText('运行时配置 (runtime.yaml)').closest('details');
    expect(runtimeDetails).not.toHaveAttribute('open');
    if (!runtimeDetails) throw new Error('runtime details not found');

    fireEvent.click(within(runtimeDetails).getByText('运行时配置 (runtime.yaml)'));
    fireEvent.click(within(runtimeDetails).getByRole('button', { name: '加载' }));

    const entryInput = await screen.findByLabelText('入口节点 ID');
    expect(entryInput).toHaveValue('ereuna_main');
    expect(screen.getByLabelText('工具调用格式')).toHaveValue('json');
    expect(screen.getByLabelText('并发 task 处理数，留空使用默认值')).toHaveValue(4);
    fireEvent.change(entryInput, { target: { value: 'bootstrap.coder' } });
    fireEvent.change(screen.getByLabelText('工具调用格式'), { target: { value: 'native' } });
    fireEvent.change(screen.getByLabelText('并发 task 处理数，留空使用默认值'), { target: { value: '7' } });
    fireEvent.click(within(runtimeDetails).getByRole('button', { name: '保存运行时配置' }));

    const expectedRuntime = 'engine:\n  tool_mode: native\n  max_workers: 7\nshell:\n  entry_node_id: bootstrap.coder\n';
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/runtime/raw', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ content: expectedRuntime }),
    })));

    const advancedYaml = within(runtimeDetails).getByText('高级 YAML 编辑').closest('details');
    expect(advancedYaml).not.toHaveAttribute('open');
    const runtimeEditor = within(runtimeDetails).getByLabelText('runtime.yaml YAML 编辑器');
    expect(runtimeEditor).toHaveValue(expectedRuntime);

    const policyDetails = screen.getByText('安全策略 (policy.yaml)').closest('details');
    if (!policyDetails) throw new Error('policy details not found');
    fireEvent.click(within(policyDetails).getByText('安全策略 (policy.yaml)'));
    fireEvent.click(within(policyDetails).getByRole('button', { name: '加载' }));
    await waitFor(() => expect(within(policyDetails).getByLabelText('policy.yaml YAML 编辑器')).toHaveValue('rules: []\n'));
    fireEvent.click(within(policyDetails).getByRole('button', { name: '保存策略 YAML' }));

    expect(confirmSpy).toHaveBeenCalledWith('修改安全策略可能影响系统安全性');
  });
});
