import { useEffect, useMemo, useRef, useState } from 'react';
import { useUnsavedChanges } from '../../../hooks/useUnsavedChanges';
import { usePageNotice } from '../../../hooks/usePageNotice';

import { getNodeFileRaw, getNodeFiles, makeNodeFileExample, updateNodeFileRaw, type NodeFileInfo } from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Block, Button, Field, PageShell, Select, StatusText } from './settingsControls';

export const NodeFilesSettingsPage = () => {
  const token = useSettingsStore(state => state.adminToken);
  return <NodeFilesSettingsPageEditor key={token || ""} />;
};

const NodeFilesSettingsPageEditor = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [files, setFiles] = useState<NodeFileInfo[]>([]);
  const [selected, setSelected] = useState('');
  const [content, setContent] = useState('');
  const [filter, setFilter] = useState<'all' | 'node' | 'fragment' | 'example'>('all');
  const { notice, setMessage, setLoadError, clearNotice } = usePageNotice();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState('');
  const [loaded, setLoaded] = useState(false);
  const requestVersion = useRef(0);
  const confirmDiscard = useUnsavedChanges(content !== saved, '当前文件还有未保存修改，确定离开或重新加载吗？');

  const visibleFiles = useMemo(() => files.filter((file) => {
    if (filter === 'all') return true;
    if (filter === 'example') return file.is_example;
    return file.kind === filter && !file.is_example;
  }), [files, filter]);

  const loadFiles = async () => {
    if (!adminToken || !isAuthenticated) return;
    const version = ++requestVersion.current;
    setLoading(true); setLoaded(false);
    try {
      const list = await getNodeFiles(adminToken);
      if (version !== requestVersion.current) return;
      setFiles(list);
      const nextSelected = selected && list.some(file => file.name === selected) ? selected : (list.find(file => file.name === 'qq.orchestrator.yaml')?.name || list[0]?.name || '');
      setSelected(nextSelected);
      setContent(''); setSaved('');
      const body = nextSelected ? await getNodeFileRaw(adminToken, nextSelected) : '';
      if (version !== requestVersion.current) return;
      setContent(body); setSaved(body); setLoaded(Boolean(nextSelected));
      setLoadError('');
    } catch (error) {
      if (version !== requestVersion.current) return;
      setLoadError(error instanceof Error ? error.message : '加载节点文件失败');
    } finally {
      if (version === requestVersion.current) setLoading(false);
    }
  };

  // 手动刷新才清提示：创建 example 写完结果紧接着就会重读。
  const refresh = async () => {
    if (!confirmDiscard()) return;
    clearNotice();
    await loadFiles();
  };

  useEffect(() => { void loadFiles(); return () => { ++requestVersion.current; }; }, [adminToken, isAuthenticated]);

  const selectFile = async (filename: string) => {
    if (!adminToken || saving || filename === selected || !confirmDiscard()) return;
    const version = ++requestVersion.current;
    setSelected(filename);
    setContent(''); setSaved(''); setLoading(true); setLoaded(false);
    // 换了文件，上一份的保存结果和读取失败都跟这份无关。
    clearNotice();
    try {
      const body = await getNodeFileRaw(adminToken, filename);
      if (version !== requestVersion.current) return;
      setContent(body); setSaved(body); setLoaded(true);
    } catch (error) {
      if (version !== requestVersion.current) return;
      setLoadError(error instanceof Error ? error.message : '读取文件失败');
    } finally {
      if (version === requestVersion.current) setLoading(false);
    }
  };

  const save = async () => {
    if (!adminToken || !selected || !loaded || loading || saving) return;
    setSaving(true);
    try {
      await updateNodeFileRaw(adminToken, selected, content);
      setSaved(content);
      setMessage(`已保存 ${selected}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const createExample = async () => {
    if (!adminToken || !selected || loading || saving || !confirmDiscard()) return;
    setSaving(true);
    try {
      const result = await makeNodeFileExample(adminToken, selected);
      setMessage(result.created ? `已创建 ${result.path}` : `Example 已存在：${result.path}`);
      await loadFiles();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '创建 example 失败');
    } finally {
      setSaving(false);
    }
  };

  const current = files.find(file => file.name === selected);

  return (
    <PageShell authed={isAuthenticated} description="管理 config/nodes 下的 YAML 节点与 Markdown 片段，包括 _persona.example.md、_persona.md、qq.orchestrator.yaml 等。" title="节点文件">
      <Block frame="boxed" title="config/nodes 文件编辑器" hint="可编辑 .yaml/.yml/.md。建议长期自定义内容放入非 example 文件；example 文件可作为模板同步。">
        <div className="mb-3 grid gap-3 md:grid-cols-[var(--duties-split-columns-narrow)]">
          <div>
            <Field label="过滤" layout="stacked">
              <Select width="wide" aria-label="文件类型过滤" onChange={(event) => setFilter(event.currentTarget.value as any)} value={filter}>
                <option value="all">全部</option>
                <option value="node">节点 YAML</option>
                <option value="fragment">片段 MD</option>
                <option value="example">Example 模板</option>
              </Select>
            </Field>
          </div>
          <div>
            <Field label="文件" layout="stacked">
              <Select width="wide" aria-label="当前文件" disabled={saving} onChange={(event) => void selectFile(event.currentTarget.value)} value={selected}>
                {selected && !visibleFiles.some(file => file.name === selected) && <option value={selected}>{selected} · 当前编辑</option>}
                {visibleFiles.map(file => (
                  <option key={file.name} value={file.name}>{file.name}{file.is_example ? ' · example' : ''}</option>
                ))}
              </Select>
            </Field>
          </div>
        </div>
        {current && <p className="mb-3 font-mono text-mini text-[var(--duties-tertiary)]">{current.path} · {current.kind === 'fragment' ? 'Markdown 片段' : '节点 YAML'} · {current.size} bytes</p>}
        <textarea
          aria-label="文件内容"
          disabled={loading || saving || !selected || !loaded}
          className="h-[var(--duties-pane-height-tall)] w-full resize-y border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 font-mono text-xs leading-5 text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
          onChange={(event) => setContent(event.currentTarget.value)}
          spellCheck={false}
          value={content}
        />
        <div className="mt-3 flex flex-wrap gap-2">
          <Button tone="quiet" disabled={loading || saving} onClick={() => void refresh()}>{loading ? '正在刷新…' : '刷新列表'}</Button>
          <Button disabled={!selected || !loaded || loading || saving || content === saved} onClick={save}>{saving ? '正在保存…' : '保存当前文件'}</Button>
          <Button tone="quiet" disabled={!selected || loading || saving || current?.is_example} onClick={createExample}>从已保存文件创建 Example</Button>
        </div>
        <StatusText message={notice} />
      </Block>
    </PageShell>
  );
};
