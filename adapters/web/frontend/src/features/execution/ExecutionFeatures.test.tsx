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

const reads = (path: string) => vi.mocked(featureRequest).mock.calls.filter(([called, options]) => called === path && !options?.method).length;
const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

describe('polling waits for the round already in flight', () => {
  afterEach(() => vi.useRealTimers());

  it('sends no further plan reads while one is pending and resumes once it settles', async () => {
    vi.useFakeTimers();
    const pending: Array<(value: unknown) => void> = [];
    vi.mocked(featureRequest).mockImplementation(async path => path.endsWith('/tools') ? { tools: [] } : new Promise(resolve => pending.push(resolve)));
    render(<ExecutionPage />);
    await tick(0);
    // The mount read holds the same latch as the interval, so no tick opens a round of its own.
    expect(reads('/v1/execution/plans')).toBe(1);
    await tick(3000);
    expect(reads('/v1/execution/plans')).toBe(1);

    await tick(9000);
    expect(reads('/v1/execution/plans')).toBe(1);

    await act(async () => { pending.forEach(resolve => resolve({ plans: [initial()] })); await vi.advanceTimersByTimeAsync(0); });
    expect(reads('/v1/execution/plans')).toBe(1);
    await tick(3000);
    expect(reads('/v1/execution/plans')).toBe(2);
  });

  it('opens the plan read before the tool list and keeps the tool list off the latch', async () => {
    vi.useFakeTimers();
    vi.mocked(featureRequest).mockImplementation(async path => path.endsWith('/tools') ? { tools: [] } : new Promise(() => undefined));
    render(<ExecutionPage />);
    await tick(0);
    expect(vi.mocked(featureRequest).mock.calls.map(([path]) => path)).toEqual(['/v1/execution/plans', '/v1/execution/tools']);

    await tick(12000);
    expect(vi.mocked(featureRequest).mock.calls.map(([path]) => path)).toEqual(['/v1/execution/plans', '/v1/execution/tools']);
  });

  it('lands a plan list even when every response outlasts the interval', async () => {
    vi.useFakeTimers();
    let round = 0;
    vi.mocked(featureRequest).mockImplementation(async path => {
      if (path.endsWith('/tools')) return { tools: [] };
      const goal = `第 ${++round} 轮目标`;
      await new Promise(resolve => { setTimeout(resolve, 8000); });
      return { plans: [{ ...initial(), goal }] };
    });
    render(<ExecutionPage />);
    expect(screen.getByText('正在读取计划…')).toBeInTheDocument();

    // 8s responses against a 3s interval: the mount read keeps the latch until it lands, and every round after it lands too.
    await tick(8000);
    expect(screen.queryByText('正在读取计划…')).toBeNull();
    expect(screen.getByRole('button', { name: '第 1 轮目标 · 待确认' })).toBeInTheDocument();
    expect(reads('/v1/execution/plans')).toBe(1);

    await tick(9000);
    expect(screen.getByRole('button', { name: '第 2 轮目标 · 待确认' })).toBeInTheDocument();
    expect(reads('/v1/execution/plans')).toBe(2);

    await tick(9000);
    expect(screen.getByRole('button', { name: '第 3 轮目标 · 待确认' })).toBeInTheDocument();
    expect(reads('/v1/execution/plans')).toBe(3);
  });

  it('sends no plan reads while a plan mutation is in flight', async () => {
    vi.useFakeTimers();
    let finish!: (value: unknown) => void;
    vi.mocked(featureRequest).mockImplementation(async (path, options) => {
      if (path.endsWith('/tools')) return { tools: [] };
      if (options?.method === 'POST') return new Promise(resolve => { finish = resolve; });
      return { plans: [] };
    });
    render(<ExecutionPage />);
    await tick(0);
    fireEvent.change(screen.getByLabelText('目标'), { target: { value: '保存报表' } });
    fireEvent.click(screen.getByRole('button', { name: '生成计划预览' }));
    await tick(0);
    expect(reads('/v1/execution/plans')).toBe(1);

    await tick(9000);
    expect(reads('/v1/execution/plans')).toBe(1);

    // The write itself re-reads once it finishes, then the interval takes over again.
    await act(async () => { finish(initial()); await vi.advanceTimersByTimeAsync(0); });
    expect(reads('/v1/execution/plans')).toBe(2);
    await tick(3000);
    expect(reads('/v1/execution/plans')).toBe(3);
  });

  it('lets a finished write re-read while the mount read still holds the latch', async () => {
    vi.useFakeTimers();
    let finish!: (value: unknown) => void;
    vi.mocked(featureRequest).mockImplementation(async (path, options) => {
      if (path.endsWith('/tools')) return { tools: [] };
      if (options?.method === 'POST') return new Promise(resolve => { finish = resolve; });
      return new Promise(() => undefined);
    });
    render(<ExecutionPage />);
    await tick(0);
    fireEvent.change(screen.getByLabelText('目标'), { target: { value: '保存报表' } });
    fireEvent.click(screen.getByRole('button', { name: '生成计划预览' }));
    await tick(9000);
    expect(reads('/v1/execution/plans')).toBe(1);

    // The write re-reads through refresh directly, past the latch the never-settling mount read still holds.
    await act(async () => { finish(initial()); await vi.advanceTimersByTimeAsync(0); });
    expect(reads('/v1/execution/plans')).toBe(2);

    await tick(9000);
    expect(reads('/v1/execution/plans')).toBe(2);
  });

  it('sends no further progress reads while the card is waiting for its first one', async () => {
    vi.useFakeTimers();
    let release!: (value: unknown) => void;
    vi.mocked(featureRequest).mockImplementation(() => new Promise(resolve => { release = resolve; }));
    render(<ExecutionProgressCard planId={initial().id} scope="web:console" />);
    expect(featureRequest).toHaveBeenCalledTimes(1);

    await tick(12000);
    expect(featureRequest).toHaveBeenCalledTimes(1);
    expect(screen.getByText('正在读取计划进度…')).toBeInTheDocument();

    await act(async () => { release(initial()); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('保存报表 · 待确认')).toBeInTheDocument();
    await tick(3000);
    expect(featureRequest).toHaveBeenCalledTimes(2);
  });

  it('keeps the card to one progress read in flight after its first one lands', async () => {
    vi.useFakeTimers();
    let round = 0;
    vi.mocked(featureRequest).mockImplementation(async () => {
      const goal = `第 ${++round} 轮进度`;
      if (round > 1) await new Promise(resolve => { setTimeout(resolve, 7000); });
      return { ...initial(), goal };
    });
    render(<ExecutionProgressCard planId={initial().id} scope="web:console" />);
    await tick(0);
    expect(screen.getByText('第 1 轮进度 · 待确认')).toBeInTheDocument();

    await tick(3000);
    expect(featureRequest).toHaveBeenCalledTimes(2);
    await tick(6000);
    expect(featureRequest).toHaveBeenCalledTimes(2);
    expect(screen.getByText('第 1 轮进度 · 待确认')).toBeInTheDocument();

    await tick(1000);
    expect(screen.getByText('第 2 轮进度 · 待确认')).toBeInTheDocument();
    await tick(2000);
    expect(featureRequest).toHaveBeenCalledTimes(3);
  });

  it('sends no further reminder reads while one is pending and resumes once it settles', async () => {
    vi.useFakeTimers();
    const pending: Array<(value: unknown) => void> = [];
    vi.mocked(featureRequest).mockImplementation(() => new Promise(resolve => pending.push(resolve)));
    render(<RemindersPage embedded />);
    await tick(0);
    expect(reads('/v1/reminders')).toBe(1);
    await tick(5000);
    expect(reads('/v1/reminders')).toBe(1);

    await tick(15000);
    expect(reads('/v1/reminders')).toBe(1);

    await act(async () => { pending.forEach(resolve => resolve({ reminders: [] })); await vi.advanceTimersByTimeAsync(0); });
    expect(reads('/v1/reminders')).toBe(1);
    await tick(5000);
    expect(reads('/v1/reminders')).toBe(2);
  });

  it('lands a reminder list even when every response outlasts the interval', async () => {
    vi.useFakeTimers();
    let round = 0;
    vi.mocked(featureRequest).mockImplementation(async () => {
      const text = `第 ${++round} 轮提醒`;
      await new Promise(resolve => { setTimeout(resolve, 12000); });
      return { reminders: [{ id: 'R123456789abc', text, due_at: '2030-01-01T00:00:00Z', timezone: 'Asia/Shanghai', revision: 1, status: 'open', delivery_status: 'delivered' }] };
    });
    render(<RemindersPage embedded />);
    expect(screen.getByText('正在读取提醒…')).toBeInTheDocument();

    // 12s responses against a 5s interval: the mount read keeps the latch until it lands, and every round after it lands too.
    await tick(12000);
    expect(screen.queryByText('正在读取提醒…')).toBeNull();
    expect(screen.getByText('第 1 轮提醒')).toBeInTheDocument();
    expect(reads('/v1/reminders')).toBe(1);

    await tick(15000);
    expect(screen.getByText('第 2 轮提醒')).toBeInTheDocument();
    expect(reads('/v1/reminders')).toBe(2);

    await tick(15000);
    expect(screen.getByText('第 3 轮提醒')).toBeInTheDocument();
    expect(reads('/v1/reminders')).toBe(3);
  });

  it('sends no reminder reads while a reminder is being created', async () => {
    vi.useFakeTimers();
    let finish!: (value: unknown) => void;
    vi.mocked(featureRequest).mockImplementation(async (_path, options) => options?.method === 'POST' ? new Promise(resolve => { finish = resolve; }) : { reminders: [] });
    render(<RemindersPage embedded />);
    await tick(0);
    fireEvent.change(screen.getByLabelText('提醒内容'), { target: { value: '交材料' } });
    fireEvent.change(screen.getByLabelText('当地时间'), { target: { value: '2030-01-01T10:00' } });
    fireEvent.click(screen.getByRole('button', { name: '创建提醒' }));
    await tick(0);
    expect(reads('/v1/reminders')).toBe(1);

    await tick(15000);
    expect(reads('/v1/reminders')).toBe(1);

    await act(async () => { finish({}); await vi.advanceTimersByTimeAsync(0); });
    expect(reads('/v1/reminders')).toBe(2);
    await tick(5000);
    expect(reads('/v1/reminders')).toBe(3);
  });
});
