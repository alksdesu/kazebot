import { useState } from 'react';
import { errorMessage } from '../../api/request';
import { NON_EMPTY_TEXT, NUMBER, TEXT, optional, readReceipt, receipted, shaped, shapedRows } from '../../api/shape';
import { Block, Button, ErrorText } from '../../components/settings/pages/settingsControls';
import { downloadFeature, featureRequest } from '../client';
import { appendSentence } from '../../utils/sentence';

interface Check { name: string; status: string; detail: string; action: string; latency_ms?: number }
interface Report { id: string; created_at: string; status: string; checks: Check[] }
const LABELS: Record<string, string> = { passed: '正常', failed: '异常', warning: '需关注', skipped: '未检查' };
const REPORT = '诊断已经做完，可报告';
const CHECK = { name: TEXT, status: TEXT, detail: TEXT, action: TEXT, latency_ms: optional(NUMBER) };

function readReport(value: unknown): Report {
  const report = shaped(REPORT, value, { id: NON_EMPTY_TEXT, created_at: TEXT, status: TEXT });
  return { id: report.id, created_at: report.created_at, status: report.status, checks: shapedRows(REPORT, report.checks, CHECK, 'checks') };
}

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
      const { result, receiptProblem } = await receipted(featureRequest('/v1/operations/diagnostics', {
        method: 'POST', receipt: REPORT, body: { test_model: testModel, include_logs: includeLogs },
      }).then(value => readReceipt(REPORT, value, readReport)));
      // 检查已经跑过了，只是报告读不懂：再点一次会重新检查一遍，勾了模型请求的还要再计一次费。
      if (!result) { setError(appendSentence(receiptProblem, '这次的结果显示不了，要看结果只能再诊断一次')); return; }
      setReport(result); setOptions(submitted); setStale(false);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  return <Block frame="boxed" title="一键诊断" hint="检查连接、工作进程、队列、存储和文档处理能力，导出报告供排查。">
    <div className="flex flex-wrap gap-3 text-sm">
      <label><input type="checkbox" disabled={busy} checked={testModel} onChange={event => setTestModel(event.target.checked)} /> 发起一次最小模型请求（可能计费）</label>
      <label><input type="checkbox" disabled={busy} checked={includeLogs} onChange={event => setIncludeLogs(event.target.checked)} /> 附带脱敏日志</label>
    </div>
    <div className="my-3 flex flex-wrap gap-2">
      <Button disabled={busy} onClick={() => void run()}>{busy ? '正在检查…' : '开始诊断'}</Button>
      {report && <Button tone="quiet" disabled={busy} onClick={() => void downloadFeature(`/v1/operations/diagnostics/${report.id}/export`, `diagnostics-${report.id}.json`).catch(failure => setError(errorMessage(failure)))}>导出报告</Button>}
    </div>
    {error && <ErrorText role="alert">{error}</ErrorText>}
    {stale && <p role="status">下方保留的是上一份诊断报告；本次诊断{busy ? '仍在进行' : '的结果没能显示'}。</p>}
    {report && <div aria-live="polite" className="space-y-3">
      <p className="text-xs text-[var(--duties-secondary)]">本报告：{options.testModel ? '包含最小模型请求' : '未请求模型'} · {options.includeLogs ? '附带脱敏日志' : '未附日志'}</p>
      <p>{new Date(report.created_at).toLocaleString()} · {LABELS[report.status] || report.status}</p>
      {report.checks.map(check => <div key={check.name} className="border-t border-[var(--duties-border)] pt-2 text-sm">
        <strong>{check.name} · {LABELS[check.status] || check.status}</strong>
        <p>{check.detail}{check.latency_ms !== undefined ? ` · ${check.latency_ms} ms` : ''}</p>
        {check.status !== 'passed' && check.action && <p className="text-[var(--duties-secondary)]">{check.action}</p>}
      </div>)}
    </div>}
  </Block>;
}
