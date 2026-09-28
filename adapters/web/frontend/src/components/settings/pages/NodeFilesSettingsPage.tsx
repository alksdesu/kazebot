import { useEffect, useMemo, useRef, useState } from 'react';
import { useUnsavedChanges } from '../../../hooks/useUnsavedChanges';
import { usePageNotice } from '../../../hooks/usePageNotice';

import { deleteNodeFile, getNodeFileRaw, getNodeFiles, updateNodeFileRaw, type NodeFileInfo } from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { overlaySourceLabel, removalFor } from '../../../utils/overlaySource';
import { Block, Button, Field, PageShell, Select, StatusText } from './settingsControls';

type FileFilter = 'all' | 'node' | 'fragment' | 'example' | 'changed';

const FILTERS: Array<{ key: FileFilter; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'node', label: '节点 YAML' },
  { key: 'fragment', label: '片段 MD' },
  { key: 'example', label: 'Example 模板' },
  { key: 'changed', label: '本实例改过或自建的' },
];

function matchesFilter(file: NodeFileInfo, filter: FileFilter): boolean {
  if (filter === 'all') return true;
  if (filter === 'changed') return file.source !== 'default';
  if (filter === 'example') return file.is_example;
  return file.kind === filter && !file.is_example;
}

function fileOptionLabel(file: NodeFileInfo): string {
  const tags = [overlaySourceLabel(file.source, file.same_as_default), file.is_example ? 'example' : ''].filter(Boolean);
  return tags.length ? `${file.name} · ${tags.join(' · ')}` : file.name;
}

export const NodeFilesSettingsPage = () => {
  const token = useSettingsStore(state => state.adminToken);
  return <NodeFilesSettingsPageEditor key={token || ""} />;
};

const NodeFilesSettingsPageEditor = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [files, setFiles] = useState<NodeFileInfo[]>([]);
  const [selected, setSelected] = useState('');
  const [content, setContent] = useState('');
  const [filter, setFilter] = useState<FileFilter>('all');
  const { notice, setMessage, setLoadError, clearNotice } = usePageNotice();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState('');
  const [loaded, setLoaded] = useState(false);
  const requestVersion = useRef(0);
  const confirmDiscard = useUnsavedChanges(content !== saved, '当前文件还有未保存修改，确定离开或重新加载吗？');

  const visibleFiles = useMemo(() => files.filter((file) => matchesFilter(file, filter)), [files, filter]);

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

  // 手动刷新才清提示：恢复默认写完结果紧接着就会重读。
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
      // 保存仓库默认会在 config/nodes 下落一份本实例的，列表里的来源和路径跟着变。
      if (files.find(file => file.name === selected)?.source === 'default') await loadFiles();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const current = files.find(file => file.name === selected);
  const removal = current ? removalFor(current.source, `文件 ${current.name}`) : null;

  const remove = async () => {
    if (!adminToken || !current || !removal || loading || saving) return;
    const unsaved = content !== saved ? '当前还有未保存修改，也会一起丢掉。' : '';
    if (!window.confirm(`${removal.confirm}${unsaved}`)) return;
    setSaving(true);
    try {
      await deleteNodeFile(adminToken, current.name);
      // 恢复默认后文件还在，只是换回仓库那份；重读列表时照旧选中它。
      if (!removal.restores) setSelected('');
      setSaved(content);
      setMessage(removal.done);
      await loadFiles();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : `${removal.label}失败`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <PageShell authed={isAuthenticated} description="节点 YAML 与 Markdown 片段，包括 _persona.md、qq.orchestrator.yaml 等。仓库自带的默认文件直接生效并跟着代码更新；保存后会在 config/nodes 下存一份本实例的版本，从此不再随仓库变化，恢复默认即删掉这份。" title="节点文件">
      <Block frame="boxed" title="节点文件编辑器" hint="可编辑 .yaml/.yml/.md。只改需要改的文件：改过的文件会盖住仓库默认，以后仓库对它的更新到不了这个实例。">
        <div className="mb-3 grid gap-3 md:grid-cols-[var(--duties-split-columns-narrow)]">
          <div>
            <Field label="过滤" layout="stacked">
              <Select width="wide" aria-label="文件类型过滤" onChange={(event) => setFilter(event.currentTarget.value as FileFilter)} value={filter}>
                {FILTERS.map(item => <option key={item.key} value={item.key}>{item.label}</option>)}
              </Select>
            </Field>
          </div>
          <div>
            <Field label="文件" layout="stacked">
              <Select width="wide" aria-label="当前文件" disabled={saving} onChange={(event) => void selectFile(event.currentTarget.value)} value={selected}>
                {selected && !visibleFiles.some(file => file.name === selected) && <option value={selected}>{selected} · 当前编辑</option>}
                {visibleFiles.map(file => (
                  <option key={file.name} value={file.name}>{fileOptionLabel(file)}</option>
                ))}
              </Select>
            </Field>
          </div>
        </div>
        {current && <p className="mb-3 font-mono text-mini text-[var(--duties-tertiary)]">{current.path} · {current.kind === 'fragment' ? 'Markdown 片段' : '节点 YAML'} · {current.size} bytes · {overlaySourceLabel(current.source, current.same_as_default)}</p>}
        {current?.same_as_default && (
          <p className="mb-3 text-mini leading-4 text-[var(--duties-secondary)]">这份和仓库默认一字不差，什么也没改，只会挡住以后仓库对它的更新，建议恢复默认。</p>
        )}
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
          <Button tone={removal?.restores ? 'quiet' : 'danger'} disabled={!removal || loading || saving} onClick={() => void remove()}>{removal?.label ?? '恢复默认'}</Button>
        </div>
        <StatusText message={notice} />
      </Block>
    </PageShell>
  );
};
