// 客户端设置：只存 localStorage 的前端偏好，不碰后端配置。
import { useEffect, useState } from 'react';

import { getConfig, getNodes } from '../../../api/supervisorClient';
import { type TitleGenerationMode, useClientPrefsStore } from '../../../store/clientPrefsStore';
import { useSettingsStore } from '../../../store/settingsStore';
import type { NodeDef } from '../../../types';
import { Block, Field, PageShell, Select } from './settingsControls';

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

const TITLE_OPTIONS: Array<{ value: TitleGenerationMode; label: string; description: string }> = [
  { value: 'auto', label: '由模型生成', description: '在支持此模式时，请助手为对话生成标题。' },
  { value: 'manual', label: '手动输入', description: '保持标题不变，直到用户手动编辑。' },
  { value: 'first-message', label: '首条消息', description: '使用首条消息文本，最多保留 50 个字符。' },
];

export const ClientSettingsPage = () => {
  const {
    titleGeneration,
    thinkingDefaultCollapsed,
    toolResultsDefaultCollapsed,
    setTitleGeneration,
    setThinkingDefaultCollapsed,
    setToolResultsDefaultCollapsed,
  } = useClientPrefsStore();
  const { adminToken, entryNodeId, setEntryNodeId } = useSettingsStore();
  const [entryNodes, setEntryNodes] = useState<NodeDef[]>([]);

  useEffect(() => {
    if (!adminToken) {
      setEntryNodes([]);
      return;
    }
    let cancelled = false;
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

  return (
    <PageShell
      description="这些偏好只保存在当前浏览器中，不会修改后端策略、共享会话状态或服务器配置。自动审批挪到了「工具与权限」页，好跟服务端策略摆在一起对照。"
      eyebrow="前端本地"
      title="客户端设置"
    >
      <Block frame="boxed" hint="新对话消息默认由此节点处理。仅显示根入口节点，不包含子节点。" scope="this-browser" title="入口节点">
        <Select
          aria-label="入口节点"
          className="font-mono text-xs"
          onChange={(e) => setEntryNodeId(e.target.value)}
          value={entryNodeId}
          width="wide"
        >
          {entryNodes.length === 0 && <option value={entryNodeId}>{entryNodeId || '正在加载…'}</option>}
          {entryNodes.map((n) => (
            <option key={n.id} value={n.id}>{n.name || n.id}{n.description ? ` — ${n.description}` : ''}</option>
          ))}
        </Select>
        <p className="mt-2 font-mono text-micro text-[var(--duties-tertiary)]">当前值：{entryNodeId || '（未设置）'}</p>
      </Block>

      <Block frame="boxed" scope="this-browser" title="对话标题">
        <Field htmlFor="client-title-generation" label="生成方式" layout="stacked">
          <Select
            aria-label="对话标题生成方式"
            className="font-mono text-xs"
            id="client-title-generation"
            onChange={(event) => setTitleGeneration(event.target.value as TitleGenerationMode)}
            value={titleGeneration}
            width="wide"
          >
            {TITLE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </Select>
        </Field>
        <p className="mt-2 text-xs leading-5 text-[var(--duties-secondary)]">
          {TITLE_OPTIONS.find((option) => option.value === titleGeneration)?.description}
        </p>
      </Block>

      <Block frame="boxed" scope="this-browser" title="消息显示">
        <div className="space-y-3">
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
      </Block>
    </PageShell>
  );
};
