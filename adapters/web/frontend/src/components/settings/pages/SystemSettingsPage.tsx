// [2026-06-02] System settings page for Supervisor status and runtime controls.
// Why: operators need one tab for health, admin state, config reload, and engine
// restart. How: combine existing health/admin endpoints with guarded action buttons
// and a 15-second status refresh. Purpose: high-impact system controls stay visible
// while still requiring admin auth and restart confirmation.
import { useCallback, useEffect, useMemo, useState } from 'react';

import { ActiveTasksModal } from '../../dashboard/ActiveTasksModal';

import { checkHealth, getAdminState, reloadConfig, restartEngine, type AdminState, type HealthState } from '../../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';
import { countActiveTasks, formatUptime } from '../../../utils/systemStatus';
import { AuthRequired, Card, PageHeader, PageShell, StatusText } from './settingsPagePrimitives';

const Stat = ({ label, value, detail, onClick }: { label: string; value: string | number; detail?: string; onClick?: () => void }) => {
  const content = (
    <>
      <p className="font-mono text-[0.6rem] uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">{label}</p>
      <p className="mt-1 font-mono text-lg font-semibold tracking-[-0.04em]">{value}</p>
      {detail && <p className="mt-1 text-xs text-[var(--duties-secondary)]">{detail}</p>}
    </>
  );
  const cls = `border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 ${onClick ? 'w-full cursor-pointer text-left transition-colors hover:bg-[var(--duties-muted)]' : ''}`;
  if (onClick) {
    return <button aria-label={`查看${label}详情`} className={cls} onClick={onClick} type="button">{content}</button>;
  }
  return <div className={cls}>{content}</div>;
};

export const SystemSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [adminState, setAdminState] = useState<AdminState | null>(null);
  const [health, setHealth] = useState<HealthState | null>(null);
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);
  const [activeTasksOpen, setActiveTasksOpen] = useState(false);
  const addSystemLog = useSettingsSelectionStore(state => state.addSystemLog);

  // 只记重载与重启，15 秒一次的状态轮询失败不进操作日志。
  const report = useCallback((text: string) => {
    setMessage(text);
    addSystemLog(text);
  }, [addSystemLog]);

  const load = useCallback(async (showSpinner = true) => {
    if (!adminToken || !isAuthenticated) return;
    if (showSpinner) setLoading(true);
    setMessage('');
    try {
      const [state, healthState] = await Promise.all([getAdminState(adminToken), checkHealth()]);
      setAdminState(state);
      setHealth(healthState);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '加载系统状态失败');
    } finally {
      if (showSpinner) setLoading(false);
    }
  }, [adminToken, isAuthenticated]);

  useEffect(() => {
    void load();
    if (!adminToken || !isAuthenticated) return undefined;
    // [2026-06-02] Refresh System status every 15 seconds.
    // Why: sessions, approvals, running tasks, workers, and uptime can change while
    // operators keep this tab open. How: run a quiet refresh without replacing the
    // manual button with a spinner. Purpose: the status cards remain current.
    const timer = window.setInterval(() => { void load(false); }, 15000);
    return () => window.clearInterval(timer);
  }, [adminToken, isAuthenticated, load]);

  const engineInfo = useMemo(() => {
    // [2026-06-02] Normalize the loose engine_runtime payload for display.
    // Why: Supervisor may report either one worker_id or a workers array. How: prefer
    // worker_id, then join string workers, then show an explicit empty value. Purpose:
    // the System tab remains useful across backend runtime schema variants.
    const runtime = adminState?.engine_runtime || {};
    const workerId = typeof runtime.worker_id === 'string' ? runtime.worker_id : '';
    const workers = Array.isArray(runtime.workers) ? runtime.workers.filter((item): item is string => typeof item === 'string') : [];
    if (workerId) return workerId;
    if (workers.length > 0) return workers.join(', ');
    return '无工作进程';
  }, [adminState]);

  const handleReload = async () => {
    if (!adminToken) return;
    try {
      await reloadConfig(adminToken);
      report('配置已重载');
      await load(false);
    } catch (error) {
      report(error instanceof Error ? error.message : '配置重载失败');
    }
  };

  const handleRestart = async () => {
    if (!adminToken) return;
    if (!window.confirm('确认要重启引擎吗？运行中的任务会全部中断，调度器与本页面的连接不受影响。')) return;
    try {
      const result = await restartEngine(adminToken);
      // scheduled=false 表示 supervisor 手上没有 engine 进程可重启，报成功会让人白等。
      report(result?.scheduled === false ? '未能重启：调度器没有在管理引擎进程' : '已提交重启请求');
    } catch (error) {
      report(error instanceof Error ? error.message : '重启失败');
    }
  };

  return (
    <PageShell>
      <PageHeader description="查看 Supervisor 运行状态，并执行配置重载或引擎重启。" title="系统" />
      {!isAuthenticated ? <AuthRequired /> : (
        <>
          <Card title="系统状态" description="数据来自管理员状态接口和健康检查接口，每 15 秒自动刷新。">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Stat label="会话数" value={adminState?.sessions ?? (loading ? '…' : '无数据')} />
              <Stat label="待审批数" value={adminState?.approvals?.pending ?? adminState?.pending_approvals?.length ?? 0} />
              <Stat label="运行中任务数" value={countActiveTasks(adminState?.tasks)} detail="运行中、等待中、已挂起" onClick={() => setActiveTasksOpen(true)} />
              <Stat label="运行时间" value={formatUptime(health?.uptime_seconds)} />
            </div>
            <div className="mt-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 text-xs leading-5">
              <p><span className="text-[var(--duties-tertiary)]">Engine worker：</span><span className="font-mono">{engineInfo}</span></p>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button disabled={loading} onClick={() => load()}>{loading ? '刷新中...' : '刷新状态'}</Button>
            </div>
          </Card>

          <ActiveTasksModal open={activeTasksOpen} onClose={() => setActiveTasksOpen(false)} />

          <Card title="运行控制" description="配置重载会重新读取配置；引擎重启需要二次确认。">
            <div className="flex flex-wrap gap-2">
              <Button onClick={handleReload} variant="primary">重载配置</Button>
              <Button onClick={handleRestart} variant="danger">重启引擎</Button>
            </div>
            <StatusText message={message} />
          </Card>
        </>
      )}
    </PageShell>
  );
};
