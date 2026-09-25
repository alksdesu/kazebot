import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ExecutionPage } from './ExecutionPage';
import { ExecutionProgressCard } from './ExecutionProgressCard';
import { RemindersPage } from './RemindersPage';
import type { ExecutionPlan } from './types';
import { downloadFeature, featureRequest } from '../client';

vi.mock('../client', () => ({ featureRequest: vi.fn(), downloadFeature: vi.fn().mockResolvedValue(undefined) }));
vi.mock('./ScopeSelect', () => ({ ScopeSelect: ({ value, onChange }: { value: string; onChange: (value: string) => void }) => <select aria-label="所属会话" value={value} onChange={event => onChange(event.target.value)}><option value="web:console">控制台</option><option value="qq_group:other">其他群</option></select> }));
vi.mock('../../api/supervisorClient', () => ({ uploadAttachment: vi.fn() }));
vi.mock('../../store/settingsStore', () => ({ useSettingsStore: (selector: (state: { adminToken: string }) => unknown) => selector({ adminToken: 'token' }) }));

const initial = (): ExecutionPlan => ({
  id: 'P123456789abc', goal: '保存报表', work_scope: '仅写入指定文件', scope: 'web:console', revision: 1, version: 1, status: 'draft',
  inputs: [{ kind: 'text', value: 'report source', label: '源资料' }],
  steps: [{ id: 'save', title: '保存', kind: 'tool', dependencies: [], input_index: null, operation: 'write_file', arguments: { path: 'report.txt', content: 'hello' }, instruction: '', status: 'pending', error: '', result: '', attempt: 0, task_id: '' }],
  items: [], artifacts: [],
});

beforeEach(() => { vi.clearAllMocks(); vi.spyOn(window, 'confirm').mockReturnValue(true); });
afterEach(() => vi.restoreAllMocks());

describe('execution feature pages', () => {
  it('shows exact inputs and operation arguments before confirmation and disables duplicate submits', async () => {
    let resolve: (value: ExecutionPlan) => void = () => undefined;
    vi.mocked(featureRequest).mockImplementation(async (path, options) => {
      if (options?.method === 'POST') return new Promise(done => { resolve = done as typeof resolve; });
      return initial();
    });
    render(<ExecutionProgressCard planId={initial().id} scope="web:console" />);
    expect(await screen.findByText('write_file', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('report.txt')).toBeInTheDocument();
    expect(screen.getByText('report source')).toBeInTheDocument();
    const button = screen.getByRole('button', { name: '确认此版本执行' });
    fireEvent.click(button);
    await waitFor(() => expect(button).toBeDisabled());
    fireEvent.click(button);
    expect(vi.mocked(featureRequest).mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
    resolve({ ...initial(), status: 'queued' });
    await screen.findByText('保存报表 · 排队中');
  });

  it('requires saving edited goal before confirming the new plan version', async () => {
    let stored: ExecutionPlan | null = null;
    vi.mocked(featureRequest).mockImplementation(async (path, options) => {
      if (path.endsWith('/tools')) return { tools: [] };
      if (options?.method === 'POST') { stored = initial(); return stored; }
      if (options?.method === 'PATCH') { stored = { ...initial(), ...(options.body as object), revision: 2 }; return stored; }
      return { plans: stored ? [stored] : [] };
    });
    render(<ExecutionPage />);
    fireEvent.change(screen.getByLabelText('目标'), { target: { value: '保存报表' } });
    fireEvent.click(screen.getByRole('button', { name: '生成计划预览' }));
    expect(await screen.findByRole('button', { name: '确认版本 1 并执行' })).toBeEnabled();
    fireEvent.change(screen.getByLabelText('目标'), { target: { value: '新的目标' } });
    expect(screen.getByRole('button', { name: '确认版本 1 并执行' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '保存修改并更新预览' }));
    expect(await screen.findByRole('button', { name: '确认版本 2 并执行' })).toBeEnabled();
  });

  it('keeps delivered reminders open and submits versioned completion', async () => {
    const reminder = { id: 'R123456789abc', text: '交材料', due_at: '2026-09-25T01:00:00+00:00', timezone: 'Asia/Shanghai', revision: 4, status: 'open', delivery_status: 'delivered', owner_label: 'QQ 123' };
    vi.mocked(featureRequest).mockImplementation(async (_path, options) => {
      if (options?.method === 'POST') { reminder.status = 'completed'; return reminder; }
      return { reminders: [reminder] };
    });
    render(<RemindersPage embedded />);
    const done = await screen.findByRole('button', { name: '完成' });
    expect(screen.getByText(/未完成 · 已送达/)).toBeInTheDocument();
    fireEvent.click(done);
    await waitFor(() => expect(screen.queryByRole('button', { name: '完成' })).not.toBeInTheDocument());
    expect(vi.mocked(featureRequest).mock.calls.some(([path, options]) => path.endsWith('/complete') && (options?.body as { expected_revision: number }).expected_revision === 4)).toBe(true);
  });

  it.each([null, { content: 'schedules: []' }, { reminders: [null] }, { reminders: [{ id: 'R123', text: 'bad', due_at: 'invalid', timezone: 'invalid', revision: 1, status: 'open', delivery_status: 'queued' }] }])('shows a recoverable error for an invalid reminder payload %j', async payload => {
    vi.mocked(featureRequest).mockResolvedValue(payload);
    render(<RemindersPage embedded />);
    expect(await screen.findByRole('alert')).toHaveTextContent('提醒列表响应格式无效');
    expect(screen.getByRole('button', { name: '创建提醒' })).toBeEnabled();
  });

  it('rejects invalid plan lists and card payloads without crashing or offering confirmation', async () => {
    vi.mocked(featureRequest).mockResolvedValue({ plans: [null] });
    const { unmount } = render(<ExecutionPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('执行计划响应格式无效');
    unmount();
    vi.mocked(featureRequest).mockResolvedValue({ ...initial(), steps: [null] });
    render(<ExecutionProgressCard planId={initial().id} scope="web:console" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('执行计划响应格式无效');
    expect(screen.queryByRole('button', { name: '确认此版本执行' })).not.toBeInTheDocument();
  });

  it('does not overwrite a newer draft revision discovered while editing', async () => {
    let stored = initial();
    let poll = () => undefined;
    const originalInterval = globalThis.setInterval;
    vi.spyOn(globalThis, 'setInterval').mockImplementation(((callback: () => void, delay: number) => {
      if (delay === 3000) { poll = callback as typeof poll; return 1; }
      return originalInterval(callback, delay);
    }) as typeof setInterval);
    vi.mocked(featureRequest).mockImplementation(async path => path.endsWith('/tools') ? { tools: [] } : { plans: [stored] });
    render(<ExecutionPage />);
    fireEvent.click(await screen.findByRole('button', { name: '保存报表 · 待确认' }));
    fireEvent.change(screen.getByLabelText('目标'), { target: { value: '本地尚未保存的目标' } });
    stored = { ...stored, revision: 2, goal: '另一位管理员的目标' };
    await act(async () => poll());
    expect(await screen.findByRole('button', { name: '保存修改并更新预览' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '确认版本 2 并执行' })).toBeDisabled();
    expect(screen.getByLabelText('目标')).toHaveValue('本地尚未保存的目标');
    fireEvent.click(screen.getByRole('button', { name: '重新加载草稿' }));
    expect(screen.getByLabelText('目标')).toHaveValue('另一位管理员的目标');
    expect(screen.getByRole('button', { name: '确认版本 2 并执行' })).toBeEnabled();
  });

  it('does not display an old scope response after switching conversations', async () => {
    let resolveOld: (value: unknown) => void = () => undefined;
    vi.mocked(featureRequest).mockImplementation(async (path, options) => {
      if (path.endsWith('/tools')) return { tools: [] };
      if (options?.scope === 'web:console') return new Promise(resolve => { resolveOld = resolve; });
      return { plans: [] };
    });
    render(<ExecutionPage />);
    fireEvent.change(screen.getByLabelText('所属会话'), { target: { value: 'qq_group:other' } });
    await act(async () => resolveOld({ plans: [initial()] }));
    expect(screen.queryByRole('button', { name: '保存报表 · 待确认' })).not.toBeInTheDocument();
  });

  it('does not expose confirmation for the previous plan while another card loads', async () => {
    vi.mocked(featureRequest).mockResolvedValueOnce(initial()).mockImplementation(() => new Promise(() => undefined));
    const { rerender } = render(<ExecutionProgressCard planId={initial().id} scope="web:console" />);
    await screen.findByRole('button', { name: '确认此版本执行' });
    rerender(<ExecutionProgressCard planId="Pabcdef123456" scope="web:console" />);
    expect(screen.queryByRole('button', { name: '确认此版本执行' })).not.toBeInTheDocument();
  });

  it('downloads one item and retries only that failed item rather than successful work', async () => {
    const artifact = { id: 'P123456789abc-save', step_id: 'save', name: 'first.md', size: 10 };
    const plan: ExecutionPlan = { ...initial(), status: 'partial', artifacts: [artifact],
      steps: [{ ...initial().steps[0], status: 'succeeded' }, { ...initial().steps[0], id: 'second', title: '第二项处理', status: 'failed', error: 'temporary failure' }],
      items: [{ index: 0, label: '第一项', status: 'succeeded', step_ids: ['save'], artifacts: [artifact] }, { index: 1, label: '第二项', status: 'failed', step_ids: ['second'], errors: ['temporary failure'] }],
    };
    vi.mocked(featureRequest).mockImplementation(async (path, options) => {
      if (path.endsWith('/tools')) return { tools: [] };
      return options?.method === 'POST' ? plan : { plans: [plan] };
    });
    render(<ExecutionPage />);
    fireEvent.click(await screen.findByRole('button', { name: '保存报表 · 部分完成' }));
    fireEvent.click(screen.getByRole('button', { name: '下载 第一项 的结果' }));
    await waitFor(() => expect(downloadFeature).toHaveBeenCalledWith(`/v1/execution/artifacts/${artifact.id}`, artifact.name, 'web:console'));
    const retry = screen.getByRole('button', { name: '只重试 第二项 的失败步骤' });
    await waitFor(() => expect(retry).toBeEnabled());
    fireEvent.click(retry);
    await waitFor(() => expect(featureRequest).toHaveBeenCalledWith(`/v1/execution/plans/${plan.id}/retry`, expect.objectContaining({ body: { step_ids: ['second'] } })));
  });
});


it('completes reminders even when the independent snooze input is empty', async () => {
  const reminder = { id: 'R1', text: '核对', due_at: '2030-01-01T00:00:00Z', timezone: 'Asia/Shanghai', revision: 2, status: 'open', delivery_status: 'delivered' };
  vi.mocked(featureRequest).mockImplementation(async (_path, options) => options?.method === 'POST' ? reminder : { reminders: [reminder] });
  render(<RemindersPage embedded />);
  await screen.findByRole('button', { name: '完成' });
  fireEvent.change(screen.getByLabelText('延后分钟数'), { target: { value: '' } });
  expect(screen.getByRole('button', { name: /延后 .*分钟/ })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '完成' }));
  await waitFor(() => expect(featureRequest).toHaveBeenCalledWith('/v1/reminders/R1/complete', expect.objectContaining({ body: expect.not.objectContaining({ minutes: expect.anything() }) })));
});

it('locks the reminder form while its submitted snapshot is saving', async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(featureRequest).mockImplementation(async (_path, options) => options?.method === 'POST' ? new Promise(resolve => { finish = resolve; }) : { reminders: [] });
  render(<RemindersPage embedded />);
  fireEvent.change(screen.getByLabelText('提醒内容'), { target: { value: '第一条' } });
  fireEvent.change(screen.getByLabelText('当地时间'), { target: { value: '2030-01-01T10:00' } });
  fireEvent.click(screen.getByRole('button', { name: '创建提醒' }));
  expect(screen.getByLabelText('提醒内容')).toBeDisabled(); expect(screen.getByLabelText('当地时间')).toBeDisabled();
  await act(async () => finish({}));
  expect(screen.getByLabelText('提醒内容')).toBeEnabled(); expect(screen.getByLabelText('提醒内容')).toHaveValue('');
});

it('keeps unsaved plan edits when a different plan is selected and discard is declined', async () => {
  vi.mocked(window.confirm).mockReturnValue(false);
  vi.mocked(featureRequest).mockImplementation(async path => path.endsWith('/tools') ? { tools: [] } : { plans: [initial(), { ...initial(), id: 'other', goal: '另一个计划' }] });
  render(<ExecutionPage />); fireEvent.click(await screen.findByRole('button', { name: '保存报表 · 待确认' }));
  fireEvent.change(screen.getByLabelText('目标'), { target: { value: '本地修改' } });
  fireEvent.click(screen.getByRole('button', { name: '另一个计划 · 待确认' }));
  expect(screen.getByLabelText('目标')).toHaveValue('本地修改');
});
