import { useCallback, useEffect, useRef, useState } from 'react';
import yaml from 'js-yaml';
import { getRuntimeFile, isEditConflict, type RuntimeFile } from '../api/supervisorClient';
import { useRequestGuard } from '../hooks/useRequestGuard';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Button, ErrorText, Footnote, Input } from './components';
import { commitRuntimeKeys, StaleKeysError } from './yamlCommit';
import { savedMessage } from '../utils/sentence';
import { errorMessage } from '../api/request';

const RETENTION_PATH = ['engine', 'context_archive', 'retention_days'];
const DEFAULT_RETENTION = 7;
const MAX_RETENTION = 3650;
const CONFLICT_MESSAGE = '归档设置已被其他页面修改，未覆盖你的草稿。请核对后重新载入归档设置。';

function retentionDays(content: string): number {
  let value: unknown = yaml.load(content);
  for (const key of RETENTION_PATH) {
    if (value === undefined || value === null) return DEFAULT_RETENTION;
    if (typeof value !== 'object' || Array.isArray(value)) throw new Error('归档配置格式不正确，请在运行时配置中检查。');
    value = (value as Record<string, unknown>)[key];
  }
  if (value === undefined) return DEFAULT_RETENTION;
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1 || value > MAX_RETENTION) throw new Error('归档保留天数须为 1～3650 的整数，请检查运行时配置。');
  return value;
}

function readSnapshot(file: RuntimeFile) {
  if (typeof file.hash !== 'string') throw new Error('服务端未返回配置版本，无法安全保存，请更新服务后重试。');
  return { ...file, days: retentionDays(file.content) };
}

type Snapshot = ReturnType<typeof readSnapshot>;

export function ContextArchiveSettings() {
  const token = useSettingsStore(state => state.adminToken);
  const requests = useRequestGuard();
  const [snapshot, setSnapshot] = useState<{ key: string; value: Snapshot } | null>(null);
  const baseline = snapshot?.key === requests.key ? snapshot.value : null;
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const mutating = useRef(false);
  const dirty = Boolean(baseline && draft !== String(baseline.days));
  const confirmDiscard = useUnsavedChanges(dirty, '归档保留设置尚未保存，确定丢弃草稿并继续吗？');
  const valid = /^\d+$/.test(draft) && Number(draft) >= 1 && Number(draft) <= MAX_RETENTION;

  // 保存和读取走同一路：保存一开始，还没回来的读取就作废。
  const load = useCallback(async () => {
    if (!token || !requests.isCurrent() || mutating.current) return;
    const request = requests.begin('archive');
    setLoading(true); setError(''); setNotice('');
    try {
      const file = await getRuntimeFile(token);
      if (!request.isCurrent()) return;
      const next = readSnapshot(file);
      setSnapshot({ key: requests.key, value: next });
      setDraft(String(next.days)); setConflict(false);
    } catch (reason) {
      if (request.isCurrent()) setError(errorMessage(reason));
    } finally {
      if (request.isCurrent()) setLoading(false);
    }
  }, [requests, token]);

  useEffect(() => {
    setSnapshot(null); setDraft(''); setError(''); setNotice(''); setConflict(false); setSaving(false);
    mutating.current = false;
    void load();
  }, [load]);

  const save = async () => {
    if (!token || !baseline || !valid || !dirty || loading || conflict || mutating.current || !requests.isCurrent()) return;
    mutating.current = true;
    const request = requests.begin('archive');
    setSaving(true); setError(''); setNotice('');
    try {
      const days = Number(draft);
      // 只写这一个键，落在本实例那层；读回来时这个键已被别处改过就不覆盖。
      const { content, result, receiptProblem } = await commitRuntimeKeys(token, [{ path: RETENTION_PATH, value: days }], {
        loaded: baseline.content,
        proceed: request.isCurrent,
      });
      if (!request.isCurrent()) return;
      if (result && !result.ok) throw new Error('未能确认保存版本，请重新载入归档设置核对；草稿已保留。');
      // 回执没读懂时指纹照旧：下一次保存先读最新一版、按原文逐键核对，不靠这里的指纹。
      setSnapshot({ key: requests.key, value: { ...baseline, content, hash: result?.hash ?? baseline.hash, source: 'override', days } });
      setDraft(String(days)); setNotice(savedMessage('归档保留设置已保存。', '', result?.warnings)); setError(receiptProblem);
    } catch (reason) {
      if (request.isCurrent()) {
        if (reason instanceof StaleKeysError) {
          setConflict(true); setError(CONFLICT_MESSAGE);
        } else if (isEditConflict(reason)) {
          setConflict(true); setError('运行时配置在保存期间发生变化，草稿已保留。请重新载入归档设置后重试。');
        } else setError(errorMessage(reason));
      }
    } finally {
      if (request.isCurrent()) {
        mutating.current = false; setSaving(false);
      }
    }
  };

  return <Block id="context-archive" title="压缩上下文归档" hint="当前实例">
    <Footnote>压缩前的上下文会存档，保留原始消息时间，供模型按时间或关键词检索当前会话的历史。</Footnote>
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <label className="text-xs" htmlFor="context-archive-retention">归档保留天数</label>
      <Input id="context-archive-retention" inputMode="numeric" value={baseline ? draft : ''} disabled={!baseline || loading || saving} onChange={event => { setDraft(event.target.value); setNotice(''); }} />
      <Button disabled={!baseline || !valid || !dirty || loading || conflict} loading={saving} onClick={() => void save()}>保存归档设置</Button>
      <Button tone="quiet" disabled={!token || saving} loading={loading} onClick={() => { if (confirmDiscard()) void load(); }}>重新载入归档设置</Button>
    </div>
    <Footnote>默认 7 天，可设置 1～3650 天。保留期从每份归档创建时开始计算；过期只清理归档，不删除当前会话或压缩摘要。此前未归档的已丢失原文无法恢复。</Footnote>
    {baseline && !valid && <ErrorText role="alert">归档保留天数须为 1～3650 的整数。</ErrorText>}
    {error && <ErrorText role="alert">{error}</ErrorText>}
    {notice && <p className="mt-2 text-xs text-[var(--duties-secondary)]" role="status">{notice}</p>}
  </Block>;
}
