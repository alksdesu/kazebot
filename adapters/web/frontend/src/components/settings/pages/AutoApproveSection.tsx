// 自动审批：勾中的工具在本浏览器免审批。存 localStorage，跟服务端策略是两回事。
import { useEffect, useMemo, useState } from 'react';

import { getAllToolNames } from '../../../api/supervisorClient';
import {
  isAutoApproveToolEnabled,
  TOOL_APPROVAL_OPERATIONS,
  useClientPrefsStore,
} from '../../../store/clientPrefsStore';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { inferToolRisk, riskClassName, riskLabel, type RiskLevel } from '../../../utils/toolRisk';
import { Button, Chip, Chips, Empty, Facts, Input, Tag } from './settingsControls';
import { Card } from './settingsPagePrimitives';

interface ToolRule {
  toolName: string;
  label: string;
  risk: RiskLevel;
  description: string;
}

const KNOWN_TOOL_RULES: Array<Omit<ToolRule, 'risk'>> = [
  { toolName: 'read_file', label: 'read_file', description: '读取项目文件。只读操作，默认自动放行。' },
  { toolName: 'search_in_files', label: 'search_in_files', description: '搜索源码文件。只读那一半自动放行；replace 模式算写入，仍然要人点。' },
  { toolName: 'list_dir', label: 'list_dir', description: '列出目录内容。只读操作，默认自动放行。' },
  { toolName: 'execute_command', label: 'execute_command', description: '执行 Shell 命令。可能影响系统，默认需要审批。' },
  { toolName: 'write_file', label: 'write_file', description: '创建或覆盖文件。只管这一个工具，改 diff、定时任务、MCP 客户端各有各的开关。' },
  { toolName: 'apply_diff', label: 'apply_diff', description: '修改现有文件。会修改工作区，默认需要审批。' },
  { toolName: 'request_restart', label: 'request_restart', description: '请求重启服务。影响运行中的服务，默认需要审批。' },
];

const RECOMMENDED_TOOL_NAMES = new Set(KNOWN_TOOL_RULES.map((rule) => rule.toolName));

const OTHER_TOOL_DESCRIPTION = '默认需要手动审批。它送审时报什么策略操作由后端决定，所以还要把那个操作也勾上才会自动放行。';

const CATALOG_LOAD_ERROR = '拉不到后端工具清单，下面只剩这几个已知项。别的工具不是被关掉了，是这次没列出来，重新加载页面再试。';

const CoveredOperations = ({ toolName }: { toolName: string }) => {
  const operations = TOOL_APPROVAL_OPERATIONS[toolName];
  if (!operations) return null;
  // 工具名和它送审时报的 policy operation 常常不同名，不写出来用户没法判断勾了到底放行什么。
  return (
    <span className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[0.65rem] text-[var(--duties-secondary)]">
      <span>覆盖操作</span>
      {operations.map((operation) => <Tag key={operation}>{operation}</Tag>)}
    </span>
  );
};

const ToolRuleToggle = ({ rule, checked, onChange }: {
  rule: ToolRule; checked: boolean; onChange: (enabled: boolean) => void;
}) => (
  <label className="flex items-start justify-between gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3">
    <span className="min-w-0">
      <span className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-semibold text-[var(--duties-text)]">{rule.label}</span>
        <span className={`rounded-sm border px-1.5 py-0.5 font-mono text-[0.55rem] uppercase tracking-[0.12em] ${riskClassName(rule.risk)}`}>
          {riskLabel(rule.risk)}
        </span>
      </span>
      <span className="mt-1 block text-xs leading-5 text-[var(--duties-secondary)]">{rule.description}</span>
      <CoveredOperations toolName={rule.toolName} />
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

function normalizeNames(names: readonly string[]): string[] {
  return [...new Set(names.map((name) => name.trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

export const AutoApproveSection = () => {
  const { autoApproveTools, setAutoApproveTool } = useClientPrefsStore();
  const adminToken = useSettingsStore((state) => state.adminToken);
  // 同页的工具清单先到就先用，免得这一节自己那次请求回来之前只剩七行在闪。
  const seededToolNames = useSettingsSelectionStore((state) => state.allToolNames);
  const [fetchedToolNames, setFetchedToolNames] = useState<string[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [query, setQuery] = useState('');
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    let active = true;
    getAllToolNames(adminToken || '')
      .then((names) => {
        if (!active) return;
        setFetchedToolNames(Array.isArray(names) ? names : []);
        setLoadFailed(false);
      })
      .catch(() => {
        if (active) setLoadFailed(true);
      });
    return () => { active = false; };
  }, [adminToken]);

  const recommended = useMemo(
    () => KNOWN_TOOL_RULES.map((rule): ToolRule => ({ ...rule, risk: inferToolRisk(rule.toolName) })),
    [],
  );

  const catalog = useMemo(
    () => normalizeNames(fetchedToolNames || seededToolNames),
    [fetchedToolNames, seededToolNames],
  );

  const others = useMemo(() => catalog
    .filter((toolName) => !RECOMMENDED_TOOL_NAMES.has(toolName))
    .map((toolName): ToolRule => ({
      toolName,
      label: toolName,
      risk: inferToolRisk(toolName),
      description: OTHER_TOOL_DESCRIPTION,
    })), [catalog]);

  const needle = query.trim().toLowerCase();
  const filtered = useMemo(
    () => (needle ? others.filter((rule) => rule.toolName.toLowerCase().includes(needle)) : others),
    [others, needle],
  );

  // 存过的名字才算数：清单里没有的旧勾选也得露出来，否则用户看不到自己放行了什么。
  const enabledNames = useMemo(
    () => Object.keys(autoApproveTools)
      .filter((toolName) => isAutoApproveToolEnabled(toolName, autoApproveTools))
      .sort((a, b) => a.localeCompare(b)),
    [autoApproveTools],
  );

  // 搜索的时候一直折着等于搜了个寂寞。
  const listOpen = showAll || Boolean(needle);
  const showLoadError = loadFailed && catalog.length === 0;

  // 收起时得连搜索词一起清掉，否则 needle 还在，列表按上面那条规则又会自己弹开。
  const toggleList = () => {
    setShowAll(!listOpen);
    if (listOpen) setQuery('');
  };

  return (
    <Card
      description="勾中的工具在这台浏览器的聊天页免审批，存在浏览器本地。QQ 和定时任务不看这里，它们只认上面那份服务端策略。"
      scope="this-browser"
      title="自动审批"
    >
      <div className="space-y-4">
        <Facts>
          一个勾只对同名的那个工具生效，而且只覆盖它下面列出的策略操作。工具送审时报的操作常常跟工具不同名
          （apply_diff、定时任务增删、MCP 客户端增删都报 write_file），所以勾 write_file 不会连带放行它们，
          它们各自要单独勾。审批卡上认不出是哪个工具时一律走人工。
        </Facts>

        <div>
          <h3 className="mb-2 font-mono text-xs font-semibold text-[var(--duties-secondary)]">
            已放行 {enabledNames.length} 项
          </h3>
          {enabledNames.length > 0 ? (
            <Chips>
              {enabledNames.map((toolName) => (
                <Chip key={toolName} onRemove={() => setAutoApproveTool(toolName, false)} removeLabel={`取消放行 ${toolName}`}>
                  {toolName}
                </Chip>
              ))}
            </Chips>
          ) : (
            <Facts>当前没有任何工具免审批，每一条都要人工点。</Facts>
          )}
        </div>

        <div>
          <h3 className="mb-2 font-mono text-xs font-semibold text-[var(--duties-secondary)]">推荐工具</h3>
          <div className="space-y-2">
            {recommended.map((rule) => (
              <ToolRuleToggle
                checked={isAutoApproveToolEnabled(rule.toolName, autoApproveTools)}
                key={rule.toolName}
                onChange={(enabled) => setAutoApproveTool(rule.toolName, enabled)}
                rule={rule}
              />
            ))}
          </div>
        </div>

        {showLoadError && <Facts>{CATALOG_LOAD_ERROR}</Facts>}

        {others.length > 0 && (
          <div>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <h3 className="font-mono text-xs font-semibold text-[var(--duties-secondary)]">
                其他工具 {others.length} 个
              </h3>
              <Input
                aria-label="搜索工具"
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索工具名"
                type="search"
                value={query}
                width="flex"
              />
              <Button onClick={toggleList} tone="quiet">
                {listOpen ? '收起' : '展开'}
              </Button>
            </div>
            {listOpen && (
              filtered.length > 0 ? (
                <div className="space-y-2">
                  {filtered.map((rule) => (
                    <ToolRuleToggle
                      checked={isAutoApproveToolEnabled(rule.toolName, autoApproveTools)}
                      key={rule.toolName}
                      onChange={(enabled) => setAutoApproveTool(rule.toolName, enabled)}
                      rule={rule}
                    />
                  ))}
                </div>
              ) : (
                <Empty>没有名字匹配「{query.trim()}」的工具。</Empty>
              )
            )}
          </div>
        )}
      </div>
    </Card>
  );
};
