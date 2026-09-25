import { useState } from 'react';
import { Button } from '../../components/common';
import { Card, StatusText } from '../../components/settings/pages/settingsPagePrimitives';
import { downloadFeature, featureRequest } from '../client';

interface Check { name: string; status: string; detail: string; action: string; latency_ms?: number }
interface Report { id: string; created_at: string; status: string; checks: Check[] }
const LABELS: Record<string, string> = { passed: '正常', failed: '异常', warning: '需关注', skipped: '未检查' };

export function DiagnosticsPanel() {
  const [busy, setBusy] = useState(false);
  const [testModel, setTestModel] = useState(false);
  const [includeLogs, setIncludeLogs] = useState(false);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState('');
  const [stale, setStale] = useState(false);
  const [options, setOptions] = useState({ testModel: false, includeLogs: false });
  async function run() {
    setBusy(true); setError(''); setStale(Boolean(report));
    const submitted = { testModel, includeLogs };
    try {
      setReport(await featureRequest<Report>('/v1/operations/diagnostics', {
        method: 'POST', body: { test_model: testModel, include_logs: includeLogs },
      })); setOptions(submitted); setStale(false);
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <Card title="一键诊断" description="检查连接、工作进程、队列、存储和文档处理能力，导出报告供排查。">
    <div className="flex flex-wrap gap-3 text-sm">
      <label><input type="checkbox" disabled={busy} checked={testModel} onChange={event => setTestModel(event.target.checked)} /> 发起一次最小模型请求（可能计费）</label>
      <label><input type="checkbox" disabled={busy} checked={includeLogs} onChange={event => setIncludeLogs(event.target.checked)} /> 附带脱敏日志</label>
    </div>
    <div className="my-3 flex flex-wrap gap-2">
      <Button variant="primary" disabled={busy} onClick={() => void run()}>{busy ? '正在检查…' : '开始诊断'}</Button>
      {report && <Button disabled={busy} onClick={() => void downloadFeature(`/v1/operations/diagnostics/${report.id}/export`, `diagnostics-${report.id}.json`).catch(failure => setError(String(failure)))}>导出报告</Button>}
    </div>
    <StatusText message={error} />
    {stale && <p role="status">下方保留的是上一份诊断报告；本次诊断{busy ? '仍在进行' : '未完成'}。</p>}
    {report && <div aria-live="polite" className="space-y-3">
      <p className="text-xs text-[var(--duties-secondary)]">本报告：{options.testModel ? '包含最小模型请求' : '未请求模型'} · {options.includeLogs ? '附带脱敏日志' : '未附日志'}</p>
      <p>{new Date(report.created_at).toLocaleString()} · {LABELS[report.status] || report.status}</p>
      {report.checks.map(check => <div key={check.name} className="border-t border-[var(--duties-border)] pt-2 text-sm">
        <strong>{check.name} · {LABELS[check.status] || check.status}</strong>
        <p>{check.detail}{check.latency_ms !== undefined ? ` · ${check.latency_ms} ms` : ''}</p>
        {check.status !== 'passed' && check.action && <p className="text-[var(--duties-secondary)]">{check.action}</p>}
      </div>)}
    </div>}
  </Card>;
}
