import { confirmNavigation } from '../../../hooks/useUnsavedChanges';
import { useExclusiveAction } from '../../../hooks/useExclusiveAction';
// Tools and permissions: which tools exist, which node may call them, and what needs approval.
// The three answers used to live on three unrelated pages, so they are stacked here instead.
import { useEffect, useRef, useState } from 'react';

import { createTool, deleteTool, getTools, reloadTools, type AdminTool } from '../../../api/supervisorClient';
import { usePageNotice } from '../../../hooks/usePageNotice';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { tokenChanged, useSettingsStore } from '../../../store/settingsStore';
import { removalFor, toolOriginLabel } from '../../../utils/overlaySource';
import { inferToolRisk, riskClassName, riskLabel } from '../../../utils/toolRisk';
import { AutoApproveSection } from './AutoApproveSection';
import { NodeGrantsSection } from './NodeGrantsSection';
import { PolicyRulesSection } from './PolicyRulesSection';
import { Block, Button, Field, Input, PageShell, StatusText } from './settingsControls';

function defaultToolScript(name: string): string {
  // [2026-06-02] Provide a safe external-tool starter file. Why: Supervisor expects
  // Python files with SPEC and output/fail helpers. How: create a minimal script that
  // returns its input and includes a modification note. Purpose: new tools are valid
  // placeholders until the operator edits their actual implementation in the right panel.
  return `# [2026-06-02] Created from Settings. Why: the web UI creates a minimal external tool. How: edit SPEC and the script body before enabling real behavior. Purpose: keep new tool files syntactically valid.\nSPEC = {\n    "name": "${name}",\n    "description": "通过设置页面创建的工具。",\n    "input_schema": {"type": "object", "properties": {}}\n}\nTIMEOUT_SEC = 30\n\noutput({"ok": True, "args": args})\n`;
}

type SourceFilter = 'all' | 'external' | 'builtin';

const SOURCE_LABEL: Record<AdminTool['source'], string> = {
  builtin: '内置',
  plugin: '插件',
  external: '外部',
};

const SOURCE_FILTERS: Array<{ key: SourceFilter; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'external', label: '外部' },
  { key: 'builtin', label: '内置' },
];

function matchesSource(tool: AdminTool, filter: SourceFilter): boolean {
  if (filter === 'all') return true;
  // 插件工具和内置工具对使用者是同一回事：都改不了源码。
  return filter === 'external' ? tool.editable : !tool.editable;
}

const ToolInventorySection = () => {
  // [2026-06-02] Pull the right-panel opener into the list page. Why: selecting a
  // tool on mobile should reveal the Python editor immediately. How: call the shared
  // settings-store setter from each row click. Purpose: users do not need a second tap
  // on the small header chevron after choosing an item.
  const { adminToken, isAuthenticated, setRightPanelOpen } = useSettingsStore();
  const { selectedTool, setAllToolNames, setSelectedTool } = useSettingsSelectionStore();
  const [tools, setTools] = useState<AdminTool[]>([]);
  const [newName, setNewName] = useState('');
  const { notice, setMessage, setLoadError, clearNotice } = usePageNotice();
  const [loading, setLoading] = useState(false);
  const { busy, run } = useExclusiveAction();
  const [query, setQuery] = useState('');
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('all');
  const generation = useRef(0);

  const load = async (): Promise<AdminTool[] | undefined> => {
    if (!adminToken || !isAuthenticated) return undefined;
    const version = ++generation.current;
    setLoading(true);
    try {
      // 授权页那份名单和这里同源，拉一次就够。
      const items = await getTools(adminToken);
      if (version !== generation.current) return undefined;
      setTools(items);
      setAllToolNames(items.map((item) => item.name));
      if (selectedTool && !items.some((item) => item.name === selectedTool.name)) setSelectedTool(null);
      setLoadError('');
      return items;
    } catch (error) {
      if (version !== generation.current) return undefined;
      setLoadError(error instanceof Error ? error.message : '加载工具失败');
      return undefined;
    } finally {
      if (version === generation.current) setLoading(false);
    }
  };

  // 只在手动刷新时清提示：新建、删除、重载写完提示紧接着就会重读。
  const refresh = async () => {
    clearNotice();
    await load();
  };

  useEffect(() => { void refresh(); return () => { generation.current += 1; }; }, [adminToken, isAuthenticated]);

  useEffect(() => {
    // [2026-06-02] Let the right-panel editor refresh parsed tool metadata after save.
    // Why: the editor is no longer inside this page. How: reload on a local browser
    // event emitted after saving raw Python. Purpose: SPEC description and timeout in
    // the list stay aligned with the file on disk.
    const handler = () => { void load(); };
    window.addEventListener('settings:tools-updated', handler);
    return () => window.removeEventListener('settings:tools-updated', handler);
  }, [adminToken, isAuthenticated, selectedTool?.name]);

  const create = () => run(async () => {
    if (!adminToken) return;
    const name = newName.trim();
    if (!name) { setMessage('请输入工具名称'); return; }
    if (!confirmNavigation()) return;
    try {
      await createTool(adminToken, { id: name, content: defaultToolScript(name) });
      if (tokenChanged(adminToken)) return;
      setNewName('');
      setMessage('工具已创建，请在右栏编辑 Python 脚本。');
      await load();
      if (tokenChanged(adminToken)) return;
      setSelectedTool({ name, source: 'external', editable: true, description: '通过设置页面创建的工具。', input_schema: { type: 'object', properties: {} }, timeout_sec: 30, has_spec: true, origin: 'custom' });
    } catch (error) {
      if (tokenChanged(adminToken)) return;
      setMessage(error instanceof Error ? error.message : '创建工具失败');
    }
  });
  // 没声明来源的外部工具是老版本后端给的，那时它们都放在可以直接删的 tools/ 下。
  const removal = selectedTool?.editable ? removalFor(selectedTool.origin ?? 'custom', `工具 ${selectedTool.name}`) : null;
  const remove = () => run(async () => {
    if (!adminToken || !selectedTool || !removal) return;
    if (!window.confirm(removal.confirm)) return;
    if (!confirmNavigation()) return;
    const toolName = selectedTool.name;
    try {
      await deleteTool(adminToken, toolName);
      if (tokenChanged(adminToken)) return;
      // 先清掉选中：恢复默认后名字没变，右栏要看到选中换过一次才会重读成仓库那份脚本。
      setSelectedTool(null);
      setMessage(`${removal.done}，重载工具后生效`);
      const items = await load();
      if (tokenChanged(adminToken) || !removal.restores) return;
      const restored = items?.find((item) => item.name === toolName);
      if (restored) setSelectedTool(restored);
    } catch (error) {
      if (tokenChanged(adminToken)) return;
      setMessage(error instanceof Error ? error.message : `${removal.label}失败`);
    }
  });
  const reload = () => run(async () => {
    if (!adminToken) return;
    if (!confirmNavigation()) return;
    try {
      const result = await reloadTools(adminToken);
      if (tokenChanged(adminToken)) return;
      setMessage(`工具已重载，序号 ${result.seq ?? '未知'}`);
      await load();
    } catch (error) {
      if (tokenChanged(adminToken)) return;
      setMessage(error instanceof Error ? error.message : '重载工具失败');
    }
  });
  const keyword = query.trim().toLowerCase();
  const visible = tools.filter((tool) => (
    matchesSource(tool, sourceFilter)
    && (!keyword
      || tool.name.toLowerCase().includes(keyword)
      || (tool.description || '').toLowerCase().includes(keyword))
  ));

  return (
    <Block frame="boxed"
      hint="模型能调的全部工具都在这里。外部脚本工具可以增删改：改仓库自带的会在 config/tools 下存一份本实例的版本，恢复默认即删掉这份；内置和插件工具只能看。风险等级根据工具名前缀推断，脚本编辑器在右栏。"
      scope="all-channels"
      title={`工具清单 · 共 ${tools.length}`}
    >
      <>
          <div className="mb-3 flex flex-wrap gap-2">
            <Button tone="quiet" disabled={loading || busy} onClick={() => void refresh()}>{loading ? '正在刷新…' : '刷新工具'}</Button>
            <Button disabled={busy} onClick={reload}>重载工具</Button>
            <Button tone={removal?.restores ? 'quiet' : 'danger'} disabled={busy || !removal} onClick={remove}>
              {removal ? `${removal.label}选中工具` : '删除选中工具'}
            </Button>
          </div>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Input width="wide"
              aria-label="筛选工具"
              className="min-w-0 flex-1"
              onChange={(event) => setQuery(event.target.value)}
              placeholder="按名字或描述筛选"
              value={query}
            />
            <div className="flex gap-1">
              {SOURCE_FILTERS.map((item) => (
                <button
                  aria-pressed={sourceFilter === item.key}
                  className={`border px-2 py-1 font-mono text-mini transition-colors ${
                    sourceFilter === item.key
                      ? 'border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)]'
                      : 'border-[var(--duties-border)] text-[var(--duties-secondary)] hover:border-[var(--duties-text)] hover:text-[var(--duties-text)]'
                  }`}
                  key={item.key}
                  onClick={() => setSourceFilter(item.key)}
                  type="button"
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>
          <ul className="m-0 max-h-[var(--duties-pane-height-tall)] list-none space-y-2 overflow-y-auto p-0">
            {visible.map((tool) => {
              const risk = inferToolRisk(tool.name);
              return (
                <li key={tool.name}>
                  <button className={`w-full border p-3 text-left ${selectedTool?.name === tool.name ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`} disabled={busy} onClick={() => { setSelectedTool(tool); setMessage(''); setRightPanelOpen(true); }} type="button">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs font-semibold">{tool.name}</span>
                      <span className={`border px-1.5 py-0.5 font-mono text-nano ${riskClassName(risk)}`}>{riskLabel(risk)}</span>
                      <span className="border border-[var(--duties-border)] px-1.5 py-0.5 font-mono text-nano text-[var(--duties-secondary)]">{SOURCE_LABEL[tool.source]}</span>
                      {tool.origin && <span className="border border-[var(--duties-border)] px-1.5 py-0.5 font-mono text-nano text-[var(--duties-secondary)]">{toolOriginLabel(tool.origin)}</span>}
                      {!tool.editable && <span className="font-mono text-nano text-[var(--duties-tertiary)]">只读</span>}
                    </div>
                    <p className="mt-1 text-xs text-[var(--duties-secondary)]">
                      {tool.description || '无描述'}
                      {tool.editable && ` · timeout ${tool.timeout_sec ?? '未设置'}`}
                    </p>
                  </button>
                </li>
              );
            })}
            {visible.length === 0 && (
              <li className="border border-dashed border-[var(--duties-border)] p-3 text-xs text-[var(--duties-tertiary)]">
                没有符合条件的工具。
              </li>
            )}
          </ul>
          <div className="mt-4 border-t border-[var(--duties-border)] pt-3">
            <Field htmlFor="new-tool-name" label="创建工具" layout="stacked">
              <Input width="wide" disabled={busy} id="new-tool-name" onChange={(event) => setNewName(event.target.value)} placeholder="工具名称" value={newName} />
              <Button className="mt-2" disabled={busy} onClick={create}>创建工具</Button>
            </Field>
          </div>
          <StatusText message={notice} />
      </>
    </Block>
  );
};

export const ToolsSettingsPage = () => {
  const { isAuthenticated } = useSettingsStore();
  return (
    <PageShell authed={isAuthenticated} description="有哪些工具、哪个节点能调、调了要不要审批。每块都标了生效范围：标「所有渠道」的连 QQ 和定时任务一起管，标「仅此浏览器」的只管你眼前这个页面。" title="工具与权限">
      <>
        <ToolInventorySection />
        <NodeGrantsSection />
        <PolicyRulesSection />
        <AutoApproveSection />
      </>
    </PageShell>
  );
};
