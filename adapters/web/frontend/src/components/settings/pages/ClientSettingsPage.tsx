// [2026-06-01] Client-only settings page.
// Why: users need frontend-local controls for approval automation, title behavior,
// and rendering defaults without changing Supervisor policy. How: bind form controls
// directly to clientPrefsStore, which persists in browser localStorage. Purpose:
// each build or browser profile can keep independent preferences.
import { useEffect, useMemo, useState } from 'react';

import { getAllToolNames, getConfig, getNodes } from '../../../api/supervisorClient';
import {
  DEFAULT_AUTO_APPROVE_TOOLS,
  type TitleGenerationMode,
  useClientPrefsStore,
} from '../../../store/clientPrefsStore';
import { useSettingsStore } from '../../../store/settingsStore';
import type { NodeDef } from '../../../types';
import { inferToolRisk, riskClassName, riskLabel, type RiskLevel } from '../../../utils/toolRisk';

export function parseNodeList(nodes: NodeDef[]): NodeDef[] {
  // [2026-06-02] Parse real Supervisor nodes into selectable entry nodes.
  // Why: the Client page must not show system nodes or delegated child workers as
  // default conversation entries. How: collect every delegate_targets reference, then
  // keep only AI nodes whose id is not system.* and is not referenced as a delegate.
  // Purpose: the first Client setting presents the actual root entry-point choices.
  const delegated = new Set<string>();
  for (const n of nodes) {
    if (n.delegate_targets) {
      for (const t of n.delegate_targets) delegated.add(t);
    }
  }
  return nodes.filter(n =>
    n.type === 'ai' &&
    !n.id.startsWith('system.') &&
    !delegated.has(n.id)
  );
}

function configuredEntryNodeId(config: Awaited<ReturnType<typeof getConfig>> | null, storedEntryNodeId: string): string {
  // [2026-06-02] Prefer the backend's configured entry node when present.
  // Why: browser localStorage can be stale or empty after a new deployment. How: read
  // direct, legacy/default, and nested shell entry-node fields from /v1/config, then
  // fall back to the settings store value. Purpose: the selected option reflects real
  // Supervisor configuration whenever that endpoint exposes it.
  return String(config?.entry_node_id || config?.default_entry_node_id || config?.shell?.entry_node_id || storedEntryNodeId || '').trim();
}

interface ToolRuleRow {
  toolName: string;
  label: string;
  risk: RiskLevel;
  description: string;
}

interface KnownToolInfo {
  toolName: string;
  label: string;
  description: string;
}

const KNOWN_TOOL_RULES: KnownToolInfo[] = [
  // [2026-06-02] Keep only curated labels and Chinese descriptions here.
  // Why: risk levels are now inferred from tool-name prefixes so new backend tools do
  // not need frontend edits. How: ToolRuleToggle receives inferToolRisk(toolName) at
  // render time. Purpose: recommended rows retain helpful copy while risk badges stay
  // automatic and never depend on hard-coded levels.
  { toolName: 'read_file', label: 'read_file', description: '读取项目文件。只读操作，默认自动放行。' },
  { toolName: 'search_in_files', label: 'search_in_files', description: '搜索源码文件。只读操作，默认自动放行。' },
  { toolName: 'list_dir', label: 'list_dir', description: '列出目录内容。只读操作，默认自动放行。' },
  { toolName: 'execute_command', label: 'execute_command', description: '执行 Shell 命令。可能影响系统，默认需要审批。' },
  { toolName: 'write_file', label: 'write_file', description: '创建或覆盖文件。会修改工作区，默认需要审批。' },
  { toolName: 'apply_diff', label: 'apply_diff', description: '修改现有文件。会修改工作区，默认需要审批。' },
  { toolName: 'request_restart', label: 'request_restart', description: '请求重启服务。影响运行中的服务，默认需要审批。' },
];

const RECOMMENDED_TOOL_NAMES = new Set(KNOWN_TOOL_RULES.map((rule) => rule.toolName));

const TITLE_OPTIONS: Array<{ value: TitleGenerationMode; label: string; description: string }> = [
  { value: 'auto', label: '由模型生成', description: '在支持此模式时，请助手为对话生成标题。' },
  { value: 'manual', label: '手动输入', description: '保持标题不变，直到用户手动编辑。' },
  { value: 'first-message', label: '首条消息', description: '使用首条消息文本，最多保留 50 个字符。' },
];

function toolListFromApi(names: string[]): string[] {
  // [2026-06-01] Why: the backend may return duplicated, empty, or unsorted names
  // as tools are registered from multiple sources. How: trim, de-duplicate, and sort
  // in one small helper. Purpose: the approval settings list remains stable and does
  // not show malformed rows from transient registry data.
  return Array.from(new Set(names.map((name) => name.trim()).filter(Boolean))).sort((a, b) => a.localeCompare(b));
}

function ToolRuleToggle({ rule, checked, onChange }: { rule: ToolRuleRow; checked: boolean; onChange: (enabled: boolean) => void }) {
  return (
    <label
      className="flex items-start justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3"
      key={rule.toolName}
    >
      <span className="min-w-0">
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-xs font-semibold text-[var(--duties-text)]">{rule.label}</span>
          <span className={`rounded-sm border px-1.5 py-0.5 font-mono text-[0.55rem] uppercase tracking-[0.12em] ${riskClassName(rule.risk)}`}>
            {riskLabel(rule.risk)}
          </span>
        </span>
        <span className="mt-1 block text-xs leading-5 text-[var(--duties-secondary)]">{rule.description}</span>
      </span>
      <input
        aria-label={`自动放行 ${rule.toolName}`}
        checked={checked}
        className="mt-1 h-4 w-4 flex-shrink-0 accent-[var(--duties-text)]"
        onChange={(event) => onChange(event.target.checked)}
        type="checkbox"
      />
    </label>
  );
}

export const ClientSettingsPage = () => {
  const {
    autoApproveTools,
    titleGeneration,
    thinkingDefaultCollapsed,
    toolResultsDefaultCollapsed,
    setAutoApproveTool,
    setTitleGeneration,
    setThinkingDefaultCollapsed,
    setToolResultsDefaultCollapsed,
  } = useClientPrefsStore();
  const { adminToken, entryNodeId, setEntryNodeId } = useSettingsStore();
  const [allToolNames, setAllToolNames] = useState<string[]>([]);
  const [entryNodes, setEntryNodes] = useState<NodeDef[]>([]);

  useEffect(() => {
    // [2026-06-01] Why: approval rules must reflect every tool registered by the
    // running Supervisor, but the API requires admin auth and can fail. How: fetch
    // on mount/token change, keep only valid names, and clear dynamic names on
    // failure. Purpose: authenticated users see the complete tool list while
    // unauthenticated users safely fall back to the recommended default rows.
    if (!adminToken) {
      setAllToolNames([]);
      setEntryNodes([]);
      return;
    }
    let cancelled = false;
    getAllToolNames(adminToken)
      .then((names) => {
        if (!cancelled) setAllToolNames(toolListFromApi(names));
      })
      .catch(() => {
        if (!cancelled) setAllToolNames([]);
      });
    Promise.all([
      getNodes(adminToken),
      getConfig().catch(() => null),
    ])
      .then(([nodes, config]) => {
        if (cancelled) return;
        const parsedNodes = parseNodeList(nodes);
        setEntryNodes(parsedNodes);
        const configured = configuredEntryNodeId(config, entryNodeId);
        // [2026-06-02] Sync the selected entry node after loading the real list.
        // Why: the select should display the backend or store default only when that
        // id exists in the filtered entry-node list. How: choose the matching id, or
        // fall back to the first root entry when no stored value exists. Purpose: the
        // first Client setting never points at a hidden child/system node.
        if (configured && parsedNodes.some((node) => node.id === configured)) {
          setEntryNodeId(configured);
        } else if (!configured && parsedNodes[0]) {
          setEntryNodeId(parsedNodes[0].id);
        }
      })
      .catch(() => {
        if (!cancelled) setEntryNodes([]);
      });
    return () => {
      cancelled = true;
    };
  }, [adminToken, setEntryNodeId]);

  const otherToolRules = useMemo(
    () => allToolNames
      .filter((toolName) => !RECOMMENDED_TOOL_NAMES.has(toolName))
      .map((toolName): ToolRuleRow => ({
        toolName,
        label: toolName,
        risk: inferToolRisk(toolName),
        description: '后端返回的其他工具。默认需要手动审批。',
      })),
    [allToolNames],
  );

  return (
    <section className="h-full min-h-0 overflow-y-auto p-4 sm:p-6">
      <div className="mx-auto max-w-3xl space-y-6">
        <header>
          <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">前端本地</p>
          <h1 className="mt-2 font-mono text-xl font-semibold tracking-[-0.04em]">客户端设置</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--duties-secondary)]">
            这些偏好只保存在当前浏览器中，不会修改后端策略、共享会话状态或服务器配置。
          </p>
        </header>

        <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
          <h2 className="font-mono text-sm font-semibold">入口节点</h2>
          <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">
            新对话消息默认由此节点处理。仅显示根入口节点，不包含子节点。
          </p>
          <select
            aria-label="入口节点"
            className="mt-3 w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-xs text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            onChange={(e) => setEntryNodeId(e.target.value)}
            value={entryNodeId}
          >
            {entryNodes.length === 0 && <option value={entryNodeId}>{entryNodeId || '加载中...'}</option>}
            {entryNodes.map((n) => (
              <option key={n.id} value={n.id}>{n.name || n.id}{n.description ? ` — ${n.description}` : ''}</option>
            ))}
          </select>
          <p className="mt-2 font-mono text-[0.6rem] text-[var(--duties-tertiary)]">当前值：{entryNodeId || '（未设置）'}</p>
        </section>

        <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
          <div className="mb-3">
            <h2 className="font-mono text-sm font-semibold">自动审批规则</h2>
            <p className="mt-1 text-xs leading-5 text-[var(--duties-secondary)]">
              低风险只读工具默认自动放行。未知工具和较高风险工具默认保持手动审批，除非在这里明确修改。
            </p>
          </div>

          <div className="space-y-4">
            <div>
              <h3 className="mb-2 font-mono text-xs font-semibold text-[var(--duties-secondary)]">推荐工具</h3>
              <div className="space-y-2">
                {KNOWN_TOOL_RULES.map((rule) => {
                  const checked = autoApproveTools[rule.toolName] ?? DEFAULT_AUTO_APPROVE_TOOLS[rule.toolName] ?? false;
                  return (
                    <ToolRuleToggle
                      checked={checked}
                      key={rule.toolName}
                      onChange={(enabled) => setAutoApproveTool(rule.toolName, enabled)}
                      rule={{ ...rule, risk: inferToolRisk(rule.toolName) }}
                    />
                  );
                })}
              </div>
            </div>

            {otherToolRules.length > 0 && (
              <div>
                <h3 className="mb-2 font-mono text-xs font-semibold text-[var(--duties-secondary)]">其他工具</h3>
                <div className="space-y-2">
                  {otherToolRules.map((rule) => {
                    const checked = autoApproveTools[rule.toolName] ?? false;
                    return (
                      <ToolRuleToggle
                        checked={checked}
                        key={rule.toolName}
                        onChange={(enabled) => setAutoApproveTool(rule.toolName, enabled)}
                        rule={rule}
                      />
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </section>


        <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
          <h2 className="font-mono text-sm font-semibold">对话标题</h2>
          <label className="mt-3 block text-xs font-semibold text-[var(--duties-secondary)]" htmlFor="client-title-generation">
            生成方式
          </label>
          <select
            aria-label="对话标题生成方式"
            className="mt-1 w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-xs text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            id="client-title-generation"
            onChange={(event) => setTitleGeneration(event.target.value as TitleGenerationMode)}
            value={titleGeneration}
          >
            {TITLE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
          <p className="mt-2 text-xs leading-5 text-[var(--duties-secondary)]">
            {TITLE_OPTIONS.find((option) => option.value === titleGeneration)?.description}
          </p>
        </section>

        <section className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
          <h2 className="font-mono text-sm font-semibold">消息显示</h2>
          <div className="mt-3 space-y-3">
            <label className="flex items-start justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3">
              <span>
                <span className="block font-mono text-xs font-semibold">默认折叠思考内容</span>
                <span className="mt-1 block text-xs leading-5 text-[var(--duties-secondary)]">启用后，新的思考内容块会默认折叠。</span>
              </span>
              <input
                aria-label="默认折叠思考内容"
                checked={thinkingDefaultCollapsed}
                className="mt-1 h-4 w-4 flex-shrink-0 accent-[var(--duties-text)]"
                onChange={(event) => setThinkingDefaultCollapsed(event.target.checked)}
                type="checkbox"
              />
            </label>

            <label className="flex items-start justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3">
              <span>
                <span className="block font-mono text-xs font-semibold">默认折叠工具结果</span>
                <span className="mt-1 block text-xs leading-5 text-[var(--duties-secondary)]">启用后，工具参数和结果详情会默认折叠。</span>
              </span>
              <input
                aria-label="默认折叠工具结果"
                checked={toolResultsDefaultCollapsed}
                className="mt-1 h-4 w-4 flex-shrink-0 accent-[var(--duties-text)]"
                onChange={(event) => setToolResultsDefaultCollapsed(event.target.checked)}
                type="checkbox"
              />
            </label>
          </div>
        </section>
      </div>
    </section>
  );
};
