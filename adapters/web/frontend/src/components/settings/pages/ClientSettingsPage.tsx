// 客户端设置：只存 localStorage 的前端偏好，不碰后端配置。
import { useEffect, useMemo, useState } from 'react';

import { useRequestGuard } from '../../../hooks/useRequestGuard';
import { type TitleGenerationMode, useClientPrefsStore } from '../../../store/clientPrefsStore';
import { settleEntryNode } from '../../../store/settingsReads';
import { useSettingsStore } from '../../../store/settingsStore';
import { rootEntryNodes } from '../../../utils/entryNodes';
import { Block, Button, ErrorText, Field, PageShell, Select } from './settingsControls';

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
  const { adminToken, availableNodes, entryNodeId, globalConfigRead, nodeListRead, setEntryNodeId } = useSettingsStore();
  const [attempt, setAttempt] = useState(0);
  const requests = useRequestGuard();
  const entryNodes = useMemo(() => rootEntryNodes(availableNodes), [availableNodes]);
  const listed = nodeListRead.loaded && entryNodes.some((node) => node.id === entryNodeId);
  const loading = Boolean(adminToken) && !nodeListRead.loaded && !nodeListRead.error;
  // 存着的入口不在清单里也单独摆一项：没有对得上的选项时浏览器显示第一项，看着像选了它，存的其实还是原来那个
  const storedLabel = entryNodeId
    ? `${entryNodeId}${nodeListRead.loaded ? '（不在入口节点列表里）' : ''}`
    : loading ? '正在加载…' : '（未设置）';
  // 全局配置只在要替人挑入口时才用得上：存着的入口还在清单里，它读没读到这里看不出差别
  const defaultEntryError = adminToken && nodeListRead.loaded && !listed ? globalConfigRead.error : '';

  // 每次进来都重读节点表和全局配置；重读不成时沿用读成过的那份，读成过就不报失败。
  useEffect(() => {
    if (!adminToken) return;
    void settleEntryNode(adminToken, requests.begin('nodes'), requests.begin('config'), () => useSettingsStore.getState().entryNodeId);
    return () => {
      requests.drop('nodes');
      requests.drop('config');
    };
  }, [requests, adminToken, attempt]);

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
          {!listed && <option value={entryNodeId}>{storedLabel}</option>}
          {nodeListRead.loaded && entryNodes.map((n) => (
            <option key={n.id} value={n.id}>{n.name || n.id}{n.description ? ` — ${n.description}` : ''}</option>
          ))}
        </Select>
        <p className="mt-2 font-mono text-micro text-[var(--duties-tertiary)]">当前值：{entryNodeId || '（未设置）'}</p>
        {adminToken && nodeListRead.error && (
          <ErrorText role="alert">
            入口节点清单读取失败：{nodeListRead.error}{' '}
            <Button onClick={() => setAttempt((value) => value + 1)} size="sm" tone="quiet">重试</Button>
          </ErrorText>
        )}
        {defaultEntryError && (
          <ErrorText role="alert">
            后端默认入口读取失败：{defaultEntryError}{' '}
            <Button onClick={() => setAttempt((value) => value + 1)} size="sm" tone="quiet">重试</Button>
          </ErrorText>
        )}
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
