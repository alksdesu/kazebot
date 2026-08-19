import { useEffect, useMemo, useState } from 'react';

import { getNodeFileRaw, getNodeFiles, makeNodeFileExample, updateNodeFileRaw, type NodeFileInfo } from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';
import { AuthRequired, Card, FieldLabel, PageHeader, PageShell, SelectInput, StatusText } from './settingsPagePrimitives';

export const NodeFilesSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [files, setFiles] = useState<NodeFileInfo[]>([]);
  const [selected, setSelected] = useState('');
  const [content, setContent] = useState('');
  const [filter, setFilter] = useState<'all' | 'node' | 'fragment' | 'example'>('all');
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const visibleFiles = useMemo(() => files.filter((file) => {
    if (filter === 'all') return true;
    if (filter === 'example') return file.is_example;
    return file.kind === filter && !file.is_example;
  }), [files, filter]);

  const loadFiles = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    try {
      const list = await getNodeFiles(adminToken);
      setFiles(list);
      const nextSelected = selected && list.some(file => file.name === selected) ? selected : (list.find(file => file.name === 'qq.orchestrator.yaml')?.name || list[0]?.name || '');
      setSelected(nextSelected);
      if (nextSelected) setContent(await getNodeFileRaw(adminToken, nextSelected));
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '加载节点文件失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void loadFiles(); }, [adminToken, isAuthenticated]);

  const selectFile = async (filename: string) => {
    if (!adminToken) return;
    setSelected(filename);
    try {
      setContent(await getNodeFileRaw(adminToken, filename));
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '读取文件失败');
    }
  };

  const save = async () => {
    if (!adminToken || !selected) return;
    try {
      await updateNodeFileRaw(adminToken, selected, content);
      setMessage(`已保存 ${selected}`);
      await loadFiles();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存失败');
    }
  };

  const createExample = async () => {
    if (!adminToken || !selected) return;
    try {
      const result = await makeNodeFileExample(adminToken, selected);
      setMessage(result.created ? `已创建 ${result.path}` : `Example 已存在：${result.path}`);
      await loadFiles();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '创建 example 失败');
    }
  };

  const current = files.find(file => file.name === selected);

  return (
    <PageShell>
      <PageHeader
        description="管理 config/nodes 下的 YAML 节点与 Markdown 片段，包括 _persona.example.md、_persona.md、qq.orchestrator.yaml 等。"
        title="节点文件"
      />
      {!isAuthenticated ? <AuthRequired /> : (
        <Card title="config/nodes 文件编辑器" description="可编辑 .yaml/.yml/.md。建议长期自定义内容放入非 example 文件；example 文件可作为模板同步。">
          <div className="mb-3 grid gap-3 md:grid-cols-[12rem_1fr]">
            <div>
              <FieldLabel>过滤</FieldLabel>
              <SelectInput onChange={(event) => setFilter(event.currentTarget.value as any)} value={filter}>
                <option value="all">全部</option>
                <option value="node">节点 YAML</option>
                <option value="fragment">片段 MD</option>
                <option value="example">Example 模板</option>
              </SelectInput>
            </div>
            <div>
              <FieldLabel>文件</FieldLabel>
              <SelectInput onChange={(event) => void selectFile(event.currentTarget.value)} value={selected}>
                {visibleFiles.map(file => (
                  <option key={file.name} value={file.name}>{file.name}{file.is_example ? ' · example' : ''}</option>
                ))}
              </SelectInput>
            </div>
          </div>
          {current && <p className="mb-3 font-mono text-[0.65rem] text-[var(--duties-tertiary)]">{current.path} · {current.kind === 'fragment' ? 'Markdown 片段' : '节点 YAML'} · {current.size} bytes</p>}
          <textarea
            className="h-[36rem] w-full resize-y border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 font-mono text-xs leading-5 text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            onChange={(event) => setContent(event.currentTarget.value)}
            spellCheck={false}
            value={content}
          />
          <div className="mt-3 flex flex-wrap gap-2">
            <Button disabled={loading} onClick={loadFiles}>{loading ? '刷新中...' : '刷新列表'}</Button>
            <Button disabled={!selected} onClick={save} variant="primary">保存当前文件</Button>
            <Button disabled={!selected || current?.is_example} onClick={createExample}>从当前文件创建 Example</Button>
          </div>
          <StatusText message={message} />
        </Card>
      )}
    </PageShell>
  );
};
