import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useSettingsStore } from '../../store/settingsStore';
import { featureRequest, featureUrl } from '../client';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { useRequestScope } from '../asyncState';
import { EmptyState, FeatureFeedback, FeaturePage, actionClass } from '../ui';
import { Block, Button, Check, Desc, ErrorText, Input, Panel, Select, Textarea } from '../../console/components';
import type { MaterialScope } from '../../api/supervisorClient';
import { scopeIdentity, type ConversationDescriptor } from '../conversationNames';
import { useConversationNames } from '../useConversationNames';

type Source = { id: string; name: string; status: string; mime_type: string; warnings?: { message: string }[] };
type Span = { id: string; text: string; citation: string; locator: { label: string } };
type Version = { id: string; artifact_id: string; number: number; status: string; sha256: string; page_count: number; format: string };
type Artifact = { id: string; name: string; current_version: string; versions: Version[] };
type Extraction = { id: string; source_id: string; revision: number; status: string; fields: { id: string; label: string; value: string; uncertain: boolean }[] };
type Job = { id: string; status: string; version_id?: string; artifact_id: string; error?: string };

const STATUS_LABELS: Record<string, string> = {
  registered: '待解析', parsed: '已解析', queued: '排队中', running: '生成与渲染中',
  preview_ready: '预览就绪，待确认', approved: '已确认', delivered: '已投递',
  failed: '失败', cancelled: '已取消', interrupted: '已中断，可重试',
  needs_review: '待校对', reviewed: '已校对',
};
const statusLabel = (status: string) => STATUS_LABELS[status] || status;

export function buildMaterialSpec(format: string, title: string, text: string): Record<string, unknown> {
  if (format === 'xlsx') return { title, sheets: [{ name: '数据', rows: text.split('\n').filter(Boolean).map(row => row.split('\t')) }] };
  if (format === 'pptx') return { title, slides: text.split(/\n\s*\n/).filter(Boolean).map(block => {
    const [heading, ...bullets] = block.split('\n');
    return { title: heading, bullets };
  }) };
  return { title, sections: text.split(/\n\s*\n/).filter(Boolean).map(block => ({ paragraphs: block.split('\n') })) };
}

export function MaterialImage({ path, scope, botScope, alt, onReady }: {
  path: string; scope: string; botScope: string | null; alt: string; onReady?: () => void;
}) {
  const token = useSettingsStore(state => state.adminToken);
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = '';
    setUrl(''); setError('');
    fetch(featureUrl(path, { scope, params: { bot_scope: botScope } }), {
      signal: controller.signal, headers: token ? { Authorization: `Bearer ${token}` } : {},
    }).then(async response => {
      if (!response.ok) throw new Error(`预览加载失败：${response.status}`);
      objectUrl = URL.createObjectURL(await response.blob());
      if (!controller.signal.aborted) setUrl(objectUrl);
      else URL.revokeObjectURL(objectUrl);
    }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [path, scope, botScope, token]);
  if (error) return <ErrorText>{error}</ErrorText>;
  return url ? <img src={url} alt={alt} className="max-w-full" onLoad={onReady} onError={() => setError('预览图片无法显示，请刷新后重试。')} /> : <p>正在读取预览…</p>;
}

export function MaterialsPage() {
  const [scope, setScope] = useState('web:materials');
  const [scopeDraft, setScopeDraft] = useState(scope);
  const [botScope, setBotScope] = useState<string | null>(null);
  const [scopeSnapshot, setScopeSnapshot] = useState<{ token: string | null; rows: MaterialScope[] }>({ token: null, rows: [] });
  const [scopeError, setScopeError] = useState('');
  const [scopeRetry, setScopeRetry] = useState(0);
  const [sources, setSources] = useState<Source[]>([]);
  const [sourceId, setSourceId] = useState('');
  const [query, setQuery] = useState('');
  const [spans, setSpans] = useState<Span[]>([]);
  const [journal, setJournal] = useState(false);
  const [chatQuery, setChatQuery] = useState('');
  const [chatResults, setChatResults] = useState<{ id: string; message_id: string; sender_name: string; timestamp: number; text: string; truncated: boolean }[]>([]);
  const [extractions, setExtractions] = useState<Extraction[]>([]);
  const [corrections, setCorrections] = useState<Record<string, { revision: number; values: Record<string, string> }>>({});
  const [artifacts, setArtifacts] = useState<{ id: string; name: string }[]>([]);
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [selectedVersion, setSelectedVersion] = useState('');
  const [compareVersion, setCompareVersion] = useState('');
  const [title, setTitle] = useState('');
  const [format, setFormat] = useState('docx');
  const [content, setContent] = useState('');
  const [job, setJob] = useState<Job | null>(null);
  const [recentJobs, setRecentJobs] = useState<Job[]>([]);
  const [inspected, setInspected] = useState(false);
  const [loadedPages, setLoadedPages] = useState<number[]>([]);
  const [capabilities, setCapabilities] = useState<{ office_renderer: boolean; modules: Record<string, boolean> } | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [searched, setSearched] = useState(false);
  const [chatSearched, setChatSearched] = useState(false);
  const [savedGeneration, setSavedGeneration] = useState('');
  const refreshSequence = useRef(0);
  const artifactSequence = useRef(0);
  const operationSequence = useRef(0);
  const runningOperation = useRef(false);
  const token = useSettingsStore(state => state.adminToken);
  const scopes = scopeSnapshot.token === token ? scopeSnapshot.rows : [];
  const options = new Map<string, ConversationDescriptor>(scopes.map(item => [scopeIdentity(item.scope, item.bot_scope), item]));
  const selectedScope = scopeIdentity(scope, botScope);
  if (!options.has(selectedScope)) options.set(selectedScope, { scope, bot_scope: botScope });
  const scopeNames = useConversationNames([...options.values()], scopeSnapshot.token === token, scopes.map(item => item.scope));
  const identity = useRequestScope(JSON.stringify([scope, botScope, token]));
  const generationDraft = JSON.stringify({ title, format, content });
  const confirmDiscard = useUnsavedChanges(Object.keys(corrections).length > 0 || Boolean((title || content) && generationDraft !== savedGeneration));
  const version = artifact?.versions.find(item => item.id === selectedVersion);
  const generationActive = Boolean(job && ['queued', 'running'].includes(job.status));
  const api = async <T,>(path: string, method = 'GET', body?: unknown, params: Record<string, string | number | boolean> = {}) => {
    identity.check();
    const result = await featureRequest<T>('/v1/materials' + path, { method, body, params: { ...params, bot_scope: botScope }, scope });
    identity.check(); return result;
  };

  async function perform(action: () => Promise<void>) {
    if (runningOperation.current) return;
    runningOperation.current = true;
    const operation = ++operationSequence.current;
    setBusy(true); setError(''); setNotice('');
    try { await action(); } catch (reason) { if (identity.isCurrent()) setError(String(reason)); }
    finally { if (operation === operationSequence.current) { runningOperation.current = false; if (identity.isCurrent()) setBusy(false); } }
  }

  function changeScope(nextScope: string, nextBot: string | null = botScope) {
    if (!nextScope.trim() || (nextScope === scope && nextBot === botScope) || !confirmDiscard()) return;
    setTitle(''); setContent(''); setSavedGeneration(''); setQuery(''); setChatQuery('');
    setScope(nextScope.trim()); setBotScope(nextBot);
  }

  async function refresh() {
    const request = ++refreshSequence.current;
    const [newSources, newArtifacts, newExtractions, settings, jobs] = await Promise.all([
      api<Source[]>('/sources'), api<{ id: string; name: string }[]>('/artifacts'),
      api<Extraction[]>('/extractions'), api<{ journal_enabled: boolean }>('/settings'),
      api<Job[]>('/jobs'),
    ]);
    if (!identity.isCurrent() || request !== refreshSequence.current) return;
    setLoading(false); setSources(newSources); setArtifacts(newArtifacts); setExtractions(newExtractions); setJournal(settings.journal_enabled); setRecentJobs(jobs);
    setJob(current => current && ['queued', 'running'].includes(current.status) ? current : jobs.find(item => ['queued', 'running'].includes(item.status)) || current);
  }

  async function openArtifact(id: string, preferred = '') {
    const request = ++artifactSequence.current;
    const item = await api<Artifact>(`/artifacts/${encodeURIComponent(id)}`);
    if (!identity.isCurrent() || request !== artifactSequence.current) return;
    setArtifact(item); setSelectedVersion(preferred || item.current_version); setCompareVersion('');
  }

  useEffect(() => {
    let active = true;
    setScopeSnapshot({ token, rows: [] }); setScopeError(''); setCapabilities(null);
    if (!token) return;
    featureRequest<MaterialScope[]>('/v1/materials/scopes').then(items => {
      if (active && useSettingsStore.getState().adminToken === token) setScopeSnapshot({ token, rows: Array.isArray(items) ? items.filter(item => item && typeof item.scope === 'string' && typeof item.bot_scope === 'string') : [] });
    }).catch(() => { if (active) setScopeError('资料会话名称读取失败，仍可操作当前范围。'); });
    featureRequest<{ office_renderer: boolean; modules: Record<string, boolean> }>('/v1/materials/capabilities').then(item => { if (active) setCapabilities(item); }).catch(reason => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, [token, scopeRetry]);

  useEffect(() => {
    operationSequence.current++; runningOperation.current = false; setBusy(false); refreshSequence.current++; artifactSequence.current++;
    setSources([]); setArtifacts([]); setExtractions([]); setJournal(false); setLoading(true); setScopeDraft(scope); setSearched(false); setChatSearched(false);
    setArtifact(null); setSelectedVersion(''); setSourceId(''); setSpans([]); setChatResults([]); setCorrections({}); setJob(null); setRecentJobs([]);
    if (scope.trim()) void perform(async () => { try { await refresh(); } finally { if (identity.isCurrent()) setLoading(false); } });
  }, [scope, botScope, token]);

  useEffect(() => { setInspected(false); setLoadedPages([]); }, [selectedVersion]);

  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.status)) return;
    let active = true;
    const timer = window.setInterval(() => {
      api<Job>(`/jobs/${job.id}`).then(next => {
        if (!active) return;
        setJob(next);
        if (next.status === 'preview_ready') {
          void perform(async () => { await refresh(); await openArtifact(next.artifact_id, next.version_id); });
        } else if (next.status === 'failed') setError(next.error || '生成失败，未发送成品。');
      }).catch(reason => { if (active) setError(String(reason)); });
    }, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [job?.id, job?.status, scope, botScope]);

  async function upload(file: File) {
    const form = new FormData(); form.append('file', file);
    identity.check();
    const response = await fetch(featureUrl('/v1/materials/sources/upload', { scope, params: { bot_scope: botScope } }), {
      method: 'POST', headers: token ? { Authorization: `Bearer ${token}` } : {}, body: form,
    });
    if (!response.ok) throw new Error((await response.text()).slice(0, 500));
    const source: Source = await response.json(); identity.check();
    await refresh(); identity.check(); setSourceId(source.id); setNotice(`已登记 ${source.name}。`);
  }

  async function generate(event: FormEvent) {
    event.preventDefault();
    await perform(async () => {
      setJob(await api<Job>('/artifacts/generate', 'POST', { format, spec: buildMaterialSpec(format, title, content) })); setSavedGeneration(generationDraft);
      setNotice('正在生成并渲染，完成后会显示全部预览页。');
    });
  }

  async function downloadVersion(item: Version, name: string) {
    const response = await fetch(featureUrl(`/v1/materials/versions/${item.id}/download`, { scope, params: { bot_scope: botScope } }), {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) throw new Error(`下载失败：${response.status}`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = `${name}.${item.format}`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return <FeaturePage title="资料与创作" description="查找文件依据与原消息，校对截图，生成并管理作品。所有内容按会话隔离，成品先预览、确认，再下载或发送。">
    <Panel>
      <div className="grid gap-4 md:grid-cols-2">
      <label>所属会话<Select width="wide" aria-label="选择已有资料会话" value={selectedScope} onChange={event => {
        const selected = options.get(event.target.value);
        if (selected) changeScope(selected.scope, selected.bot_scope ?? null);
      }}>{[...options].map(([key]) => <option key={key} value={key}>{scopeNames.get(key)}</option>)}</Select></label>
      <details><summary className="cursor-pointer text-sm">高级：会话标识</summary><form className="mt-3 min-w-0 space-y-2" onSubmit={event => { event.preventDefault(); changeScope(scopeDraft, null); }}><label>会话内部标识<Input width="wide" aria-label="资料会话范围" value={scopeDraft} onChange={event => setScopeDraft(event.target.value)} /></label><Button type="submit" disabled={!scopeDraft.trim() || (scopeDraft === scope && botScope === null)}>切换会话范围</Button></form></details>
      </div>
      <p className="break-words text-xs text-[var(--duties-secondary)]">当前操作范围：{scopeNames.get(selectedScope)}</p><p>Office 渲染：{!capabilities ? '读取中…' : capabilities.office_renderer ? '可用' : '未就绪，DOCX/XLSX/PPTX 暂不能完成预览'}；PDF 预览：{capabilities?.modules.pdf_preview ? '可用' : '未就绪'}</p>
      {scopeError && <p role="status">{scopeError} <Button tone="quiet" onClick={() => setScopeRetry(value => value + 1)}>重试会话名称</Button></p>}
      <Button disabled={busy} onClick={() => void perform(refresh)}>刷新</Button>
    </Panel>
    <FeatureFeedback error={error} notice={notice} retry={() => void perform(refresh)} />
    {loading && <p role="status">正在读取当前会话资料…</p>}
    <nav aria-label="资料页内容" className="flex flex-wrap gap-2">{[['source-section', '文件依据'], ['journal-section', '原消息'], ['extraction-section', '截图校对'], ['generation-section', '生成成品'], ['version-section', '作品预览']].map(([id, label]) => <a key={id} className={actionClass} href={`#${id}`}>{label}</a>)}</nav>
    <div id="source-section" className="scroll-mt-4"><Block title="上传并查找文件依据">
      <input aria-label="上传资料" type="file" accept=".pdf,.docx,.xlsx,.pptx,.txt,.md,.csv,.tsv,.json,.png,.jpg,.jpeg,.webp,.gif" disabled={busy} onChange={event => {
        const file = event.target.files?.[0]; if (file) void perform(() => upload(file)); event.target.value = '';
      }} />
      <Select width="wide" disabled={busy} aria-label="选择资料" value={sourceId} onChange={event => { setSourceId(event.target.value); setSpans([]); setSearched(false); }}>
        <option value="">选择文件</option>{sources.map(item => <option key={item.id} value={item.id}>{item.name} · {statusLabel(item.status)}</option>)}
      </Select>
      <Input width="wide" aria-label="文件检索词" placeholder="输入问题中的关键词；留空查看开头依据" value={query} onChange={event => setQuery(event.target.value)} />
      <Button disabled={!sourceId || busy} onClick={() => void perform(async () => {
        const found = await api<{ results: Span[]; warnings: { message: string }[] }>(`/sources/${sourceId}/spans`, 'GET', undefined, { q: query });
        setSearched(true); setSpans(found.results); setNotice(found.warnings.map(warning => warning.message).join(' '));
      })}>解析并检索</Button>
      {!loading && !sources.length && <EmptyState>尚无资料。上传文件后，可按关键词查找原文依据。</EmptyState>}
      {searched && !spans.length && <EmptyState>没有找到匹配依据，请换一个关键词或检查解析提示。</EmptyState>}
      {spans.map(span => <Panel key={span.id}><strong>{span.citation}</strong><p className="whitespace-pre-wrap">{span.text}</p><Button disabled={busy} onClick={() => void perform(async () => {
        const cite = await api<{ label: string; quote: string }>(`/sources/${sourceId}/cite`, 'POST', { span_id: span.id, quote: span.text.slice(0, 4000) });
        setNotice(`已核验原文引用：${cite.label}\n${cite.quote}`);
      })}>核验这段引用</Button></Panel>)}
    </Block></div>
    <div id="journal-section" className="scroll-mt-4"><Block title="授权原消息检索">
      <Check disabled={busy} checked={journal} onChange={checked => void perform(async () => {
        const config = await api<{ journal_enabled: boolean }>('/settings', 'PUT', { journal_enabled: checked }); setJournal(config.journal_enabled); if (!config.journal_enabled) { setChatResults([]); setChatSearched(false); }
      })}>记录当前会话的新原消息</Check>
      <Desc>开启后才记录实际收到的消息。关闭后停止检索；压缩摘要不能当作过去的原话。</Desc>
      <Input width="wide" aria-label="聊天原文检索" value={chatQuery} onChange={event => setChatQuery(event.target.value)} placeholder="原话关键词" />
      <Button disabled={!journal || busy} onClick={() => void perform(async () => {
        const found = await api<{ results: typeof chatResults; message?: string }>('/journal/search', 'GET', undefined, { q: chatQuery }); setChatSearched(true); setChatResults(found.results); if (found.message) setNotice(found.message);
      })}>查找原消息</Button>
      {chatSearched && !chatResults.length && <EmptyState>没有匹配的原消息。只会检索授权开启后收到的消息。</EmptyState>}
      {chatResults.map(item => <Panel key={item.id}><strong>{item.sender_name} · {new Date(item.timestamp * 1000).toLocaleString()} · 消息 {item.message_id}</strong><p className="whitespace-pre-wrap">{item.text}</p>{item.truncated && <Button disabled={busy} onClick={() => void perform(async () => {
        const original = await api<{ text: string }>(`/journal/messages/${encodeURIComponent(item.message_id)}`); setNotice(original.text);
      })}>查看完整原文</Button>}</Panel>)}
    </Block></div>
    <div id="extraction-section" className="scroll-mt-4"><Block title="截图结构化校对">
      <Desc>在 QQ 发截图并说明“整理成表格、待办或错误信息”，识别草稿会显示在这里。模糊字段需核对原图；校对完成不自动执行待办或命令。</Desc>
      {!loading && !extractions.length && <EmptyState>尚无识别草稿。在 QQ 发送截图并说明要整理的内容后，这里会显示可校对字段。</EmptyState>}
      {extractions.map(item => <Panel key={item.id}>
        <h3>{item.id} · 修订 {item.revision} · {statusLabel(item.status)}</h3>
        <MaterialImage path={`/v1/materials/sources/${item.source_id}/file`} scope={scope} botScope={botScope} alt="待校对截图" />
        {item.fields.map(field => <label className="block" key={field.id}>{field.label}{field.uncertain ? '（待核对）' : ''}<Input width="wide" aria-label={`${item.id} ${field.label}`} disabled={busy} value={corrections[item.id]?.values[field.id] ?? field.value} onChange={event => setCorrections(current => ({ ...current, [item.id]: { revision: current[item.id]?.revision ?? item.revision, values: { ...current[item.id]?.values, [field.id]: event.target.value } } }))} /></label>)}
        {corrections[item.id] && corrections[item.id].revision !== item.revision && <div role="alert"><p>校对已有新的修订。本地草稿已保留，请核对后重新载入。</p><Button disabled={busy} onClick={() => { if (window.confirm('丢弃这份本地校对草稿，载入最新修订？')) setCorrections(current => { const next = { ...current }; delete next[item.id]; return next; }); }}>重新载入校对</Button></div>}
        <Button disabled={busy || Boolean(corrections[item.id] && corrections[item.id].revision !== item.revision)} onClick={() => void perform(async () => { await api(`/extractions/${item.id}`, 'PATCH', { expected_revision: corrections[item.id]?.revision ?? item.revision, values: corrections[item.id]?.values || {}, confirm: true }); setCorrections(current => { const next = { ...current }; delete next[item.id]; return next; }); await refresh(); })}>保存并确认校对</Button>
      </Panel>)}
    </Block></div>
    <div id="generation-section" className="scroll-mt-4"><Block title="生成可编辑成品">
      <form onSubmit={event => void generate(event)}><fieldset disabled={busy} className="min-w-0 space-y-4">
        <div className="grid gap-4 md:grid-cols-2">
        <label>标题<Input width="wide" aria-label="成品标题" value={title} onChange={event => setTitle(event.target.value)} required /></label>
        <label>格式<Select width="wide" aria-label="成品格式" value={format} onChange={event => setFormat(event.target.value)}>{['docx', 'pdf', 'xlsx', 'pptx'].map(item => <option key={item}>{item}</option>)}</Select></label>
        </div>
        <Desc>{format === 'xlsx' ? '粘贴制表符分隔的数据，首行为表头。' : format === 'pptx' ? '每页用空行分隔；每页第一行是标题，后续每行是一条正文。' : '输入正文，空行分隔章节。需要复杂表格、公式或排版时可在 QQ 说明需求。'}</Desc>
        <Textarea rows={8} aria-label="成品内容" value={content} onChange={event => setContent(event.target.value)} required />
        <Button type="submit" disabled={busy || generationActive || !title || !content}>生成并渲染预览</Button>
      </fieldset></form>
      {job && <p role="status">任务 {job.id}：{statusLabel(job.status)} {job.error || ''}{['queued', 'running'].includes(job.status) && <Button disabled={busy} onClick={() => void perform(async () => setJob(await api<Job>(`/jobs/${job.id}/cancel`, 'POST')))}>取消</Button>}</p>}
      {job?.status === 'preview_ready' && job.version_id && <Button disabled={busy} onClick={() => void perform(() => openArtifact(job.artifact_id, job.version_id))}>查看本次生成预览</Button>}
      {recentJobs.filter(item => item.id !== job?.id).slice(0, 10).map(item => <Panel key={item.id}>
        <p>任务 {item.id}：{statusLabel(item.status)} {item.error || ''}</p>
        {['failed', 'interrupted', 'cancelled'].includes(item.status) && <Button disabled={busy} onClick={() => void perform(async () => { setJob(await api<Job>(`/jobs/${item.id}/retry`, 'POST')); await refresh(); })}>重试本地生成</Button>}
        {item.status === 'preview_ready' && item.version_id && <Button disabled={busy} onClick={() => void perform(() => openArtifact(item.artifact_id, item.version_id))}>查看已完成预览</Button>}
      </Panel>)}
      {job && ['failed', 'interrupted', 'cancelled'].includes(job.status) && <Button disabled={busy} onClick={() => void perform(async () => { setJob(await api<Job>(`/jobs/${job.id}/retry`, 'POST')); await refresh(); })}>重试本地生成</Button>}
    </Block></div>
    <div id="version-section" className="scroll-mt-4"><Block title="作品版本与预览确认">
      <Select width="wide" disabled={busy} aria-label="选择作品" value={artifact?.id || ''} onChange={event => { if (event.target.value) void perform(() => openArtifact(event.target.value)); else { artifactSequence.current++; setArtifact(null); setSelectedVersion(''); setCompareVersion(''); } }}><option value="">选择作品</option>{artifacts.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</Select>
      {!loading && !artifacts.length && <EmptyState>尚无作品。生成成品，或在 QQ 完成图片创作后，可在这里预览和管理版本。</EmptyState>}
      {artifact && <>
        <h3 className="font-semibold">{artifact.name}</h3>
        <div className="grid gap-4 md:grid-cols-2">
        <Select width="wide" disabled={busy} aria-label="选择版本" value={selectedVersion} onChange={event => { setSelectedVersion(event.target.value); setCompareVersion(''); }}>{artifact.versions.map(item => <option key={item.id} value={item.id}>v{item.number} · {statusLabel(item.status)}</option>)}</Select>
        <Select width="wide" disabled={busy} aria-label="比较版本" value={compareVersion} onChange={event => setCompareVersion(event.target.value)}><option value="">不比较</option>{artifact.versions.filter(item => item.id !== selectedVersion).map(item => <option key={item.id} value={item.id}>对比 v{item.number}</option>)}</Select>
        </div>
        {compareVersion && <div className="grid grid-cols-1 gap-4 lg:grid-cols-2"><MaterialImage path={`/v1/materials/versions/${selectedVersion}/preview/1`} scope={scope} botScope={botScope} alt="当前选择版本第一页" /><MaterialImage path={`/v1/materials/versions/${compareVersion}/preview/1`} scope={scope} botScope={botScope} alt="比较版本第一页" /></div>}
        {version && <>
          <p>v{version.number}，共 {version.page_count} 页。以下预览对应将下载或发送的确定文件。</p>
          {Array.from({ length: version.page_count }, (_, index) => <Panel key={`${version.id}:${index}`}><p>第 {index + 1} 页</p><MaterialImage path={`/v1/materials/versions/${version.id}/preview/${index + 1}`} scope={scope} botScope={botScope} alt={`预览第${index + 1}页`} onReady={() => setLoadedPages(current => current.includes(index) ? current : [...current, index])} /></Panel>)}
          {version.id !== artifact.current_version ? <Button disabled={busy} onClick={() => void perform(async () => { const restored = await api<Version>(`/artifacts/${artifact.id}/restore`, 'POST', { version_id: version.id, expected_version: artifact.current_version }); await openArtifact(artifact.id, restored.id); setNotice('已恢复为新版本，旧版本仍保留。'); })}>恢复此版为新版本</Button> : <>
            <Check checked={inspected} onChange={setInspected}>我已核对全部预览页，内容与排版无误</Check>
            <Button disabled={busy || !inspected || loadedPages.length !== version.page_count} onClick={() => void perform(async () => { await api(`/versions/${version.id}/approve`, 'POST', { sha256: version.sha256, preview_inspected: true }); await openArtifact(artifact.id, version.id); setNotice('已确认这个确定版本。'); })}>确认当前版本</Button>
            <Button disabled={busy || version.status !== 'approved'} onClick={() => void perform(() => downloadVersion(version, artifact.name))}>下载成品</Button>
            <Button disabled={busy || version.status !== 'approved'} onClick={() => void perform(async () => { const sent = await api<{ message: string }>(`/versions/${version.id}/send`, 'POST'); setNotice(sent.message); })}>发送到当前会话</Button>
          </>}
        </>}
      </>}
    </Block></div>
  </FeaturePage>;
}

export default MaterialsPage;
