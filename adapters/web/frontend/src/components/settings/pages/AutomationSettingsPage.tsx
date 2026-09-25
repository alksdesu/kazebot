// [2026-06-02] Automation settings page for schedules.yaml.
// Why: scheduled tasks are operational automation and should be visible as parsed
// rows rather than only raw YAML. How: parse schedules.yaml into a list, keep create
// and delete actions here, and render the selected task form in the Settings right
// panel. Purpose: common schedule edits become safer while raw YAML fallback remains.
import { useEffect, useRef, useState } from 'react';
import { RemindersPage } from '../../../features/execution';

import { getSchedulesRaw, updateSchedulesRaw } from '../../../api/supervisorClient';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';
import { confirmNavigation } from '../../../hooks/useUnsavedChanges';
import { AuthRequired, Card, FieldLabel, PageHeader, PageShell, StatusText, TextInput } from './settingsPagePrimitives';
import { parseSchedules, serializeSchedules, type ScheduleFormState } from '../settingsStructuredConfig';

function emptySchedule(id: string): ScheduleFormState {
  // [2026-06-02] Provide a normalized schedule draft. Why: the backend exposes only
  // raw schedules.yaml writes. How: create a message-type task with disabled false
  // defaults and required fields. Purpose: new tasks can be created from the list and
  // then completed in the right-panel form.
  return { id, cron: '0 0 * * *', type: 'message', text: '', command: '', enabled: false, once: false, conversation_key: '', entry_node_id: '', workflow_id: '', timeout: '', silent: true };
}

export const AutomationSettingsPage = () => {
  // [2026-06-02] Pull the right-panel opener into the list page. Why: selecting a
  // schedule on mobile should reveal the automation form immediately. How: call the
  // shared settings-store setter from each row click. Purpose: users do not need a
  // second tap on the small header chevron after choosing an item.
  const { adminToken, isAuthenticated, setRightPanelOpen } = useSettingsStore();
  const { selectedScheduleId, setSelectedScheduleId } = useSettingsSelectionStore();
  const [schedules, setSchedules] = useState<ScheduleFormState[]>([]);
  const [newId, setNewId] = useState('');
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const mutation = useRef(false);
  const generation = useRef(0);

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    const version = ++generation.current;
    setLoading(true);
    try {
      const parsed = parseSchedules(await getSchedulesRaw(adminToken));
      if (version !== generation.current) return;
      setSchedules(parsed);
      if (selectedScheduleId && !parsed.some((schedule) => schedule.id === selectedScheduleId)) setSelectedScheduleId(null);
    } catch (error) {
      if (version !== generation.current) return;
      setMessage(error instanceof Error ? error.message : '加载定时任务失败');
    } finally {
      if (version === generation.current) setLoading(false);
    }
  };

  useEffect(() => { void load(); return () => { generation.current += 1; }; }, [adminToken, isAuthenticated]);

  useEffect(() => {
    // [2026-06-02] Refresh schedule rows after the right-panel form saves. Why: the
    // form and list are separate settings hosts. How: listen for a local browser event
    // emitted by the right panel. Purpose: cron, type, and enabled previews stay current.
    const handler = () => { void load(); };
    window.addEventListener('settings:schedules-updated', handler);
    return () => window.removeEventListener('settings:schedules-updated', handler);
  }, [adminToken, isAuthenticated, selectedScheduleId]);

  const create = async () => {
    if (!adminToken || mutation.current || loading) return;
    const id = newId.trim();
    if (!id) { setMessage('请输入任务 ID'); return; }
    if (!confirmNavigation()) return;
    mutation.current = true; setBusy(true);
    try {
      const raw = await getSchedulesRaw(adminToken);
      const current = parseSchedules(raw);
      if (current.some((schedule) => schedule.id === id)) { setMessage('该任务 ID 已存在'); return; }
      const next = [...current, emptySchedule(id)];
      await updateSchedulesRaw(adminToken, serializeSchedules(next, raw));
      if (useSettingsStore.getState().adminToken !== adminToken) return;
      setNewId('');
      setSelectedScheduleId(id);
      setMessage('任务已创建但尚未启用，请在右栏填写内容后启用。');
      setRightPanelOpen(true);
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '创建定时任务失败');
    } finally { mutation.current = false; setBusy(false); }
  };

  const remove = async () => {
    if (!adminToken || !selectedScheduleId || mutation.current || loading) return;
    if (!window.confirm(`确定要删除定时任务 ${selectedScheduleId} 吗？`)) return;
    if (!confirmNavigation()) return;
    mutation.current = true; setBusy(true);
    try {
      const raw = await getSchedulesRaw(adminToken);
      const next = parseSchedules(raw).filter((schedule) => schedule.id !== selectedScheduleId);
      await updateSchedulesRaw(adminToken, serializeSchedules(next, raw));
      setSelectedScheduleId(null);
      setMessage('定时任务已删除');
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '删除定时任务失败');
    } finally { mutation.current = false; setBusy(false); }
  };

  return (
    <PageShell>
      <PageHeader description="查看 data/schedules.yaml 中的定时任务。cron 时间按 UTC 解释，选择任务后在右栏编辑字段。" title="自动化" />
      {isAuthenticated && <RemindersPage embedded />}
      {!isAuthenticated ? <AuthRequired /> : (
        <Card title="定时任务列表" description="每项显示 ID、cron、类型、启用状态和 text 预览。字段表单与 Raw YAML fallback 位于右栏。">
          <div className="mb-3 flex flex-wrap gap-2">
            <Button disabled={loading || busy} onClick={load}>{loading ? '刷新中...' : '刷新'}</Button>
            <Button disabled={loading || busy || !selectedScheduleId} onClick={remove} variant="danger">删除选中任务</Button>
          </div>
          <div className="max-h-[34rem] space-y-2 overflow-y-auto">
            {schedules.length === 0 ? <p className="text-sm text-[var(--duties-secondary)]">暂无定时任务。</p> : schedules.map((schedule) => (
              <button className={`w-full border p-3 text-left ${selectedScheduleId === schedule.id ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`} key={schedule.id} disabled={busy} onClick={() => { setSelectedScheduleId(schedule.id); setMessage(''); setRightPanelOpen(true); }} type="button">
                <div className="flex flex-wrap items-center gap-2"><span className="font-mono text-xs font-semibold">{schedule.id}</span><span className={`border px-1.5 py-0.5 text-xs ${schedule.enabled ? 'border-[var(--duties-border)] text-[var(--duties-success)]' : 'border-[var(--duties-border)] text-[var(--duties-secondary)]'}`}>{schedule.enabled ? '启用' : '禁用'}</span><span className="font-mono text-xs text-[var(--duties-tertiary)]">{schedule.type}</span></div>
                <p className="mt-1 font-mono text-xs text-[var(--duties-secondary)]">{schedule.cron || '未设置 cron'}</p>
                <p className="mt-1 line-clamp-2 text-xs leading-5 text-[var(--duties-secondary)]">{schedule.text || '无 text 内容'}</p>
              </button>
            ))}
          </div>
          <div className="mt-4 border-t border-[var(--duties-border)] pt-3">
            <FieldLabel htmlFor="new-schedule-id">创建定时任务</FieldLabel>
            <TextInput disabled={busy} id="new-schedule-id" onChange={(event) => setNewId(event.target.value)} placeholder="任务 ID" value={newId} />
            <Button className="mt-2" disabled={busy || loading || !newId.trim()} onClick={create} variant="primary">创建任务</Button>
          </div>
          <StatusText message={message} />
        </Card>
      )}
    </PageShell>
  );
};
