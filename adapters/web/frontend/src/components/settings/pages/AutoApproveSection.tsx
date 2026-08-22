// 自动审批：勾中的工具在本浏览器免审批。存 localStorage，跟服务端策略是两回事。
import { useMemo } from 'react';

import { DEFAULT_AUTO_APPROVE_TOOLS, useClientPrefsStore } from '../../../store/clientPrefsStore';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { inferToolRisk, riskClassName, riskLabel, type RiskLevel } from '../../../utils/toolRisk';
import { Card } from './settingsPagePrimitives';

interface ToolRule {
  toolName: string;
  label: string;
  risk: RiskLevel;
  description: string;
}

const KNOWN_TOOL_RULES: Array<Omit<ToolRule, 'risk'>> = [
  { toolName: 'read_file', label: 'read_file', description: '读取项目文件。只读操作，默认自动放行。' },
  { toolName: 'search_in_files', label: 'search_in_files', description: '搜索源码文件。只读操作，默认自动放行。' },
  { toolName: 'list_dir', label: 'list_dir', description: '列出目录内容。只读操作，默认自动放行。' },
  { toolName: 'execute_command', label: 'execute_command', description: '执行 Shell 命令。可能影响系统，默认需要审批。' },
  { toolName: 'write_file', label: 'write_file', description: '创建或覆盖文件。会修改工作区，默认需要审批。' },
  { toolName: 'apply_diff', label: 'apply_diff', description: '修改现有文件。会修改工作区，默认需要审批。' },
  { toolName: 'request_restart', label: 'request_restart', description: '请求重启服务。影响运行中的服务，默认需要审批。' },
];

const RECOMMENDED_TOOL_NAMES = new Set(KNOWN_TOOL_RULES.map((rule) => rule.toolName));

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

export const AutoApproveSection = () => {
  const { autoApproveTools, setAutoApproveTool } = useClientPrefsStore();
  const allToolNames = useSettingsSelectionStore((state) => state.allToolNames);

  const recommended = useMemo(
    () => KNOWN_TOOL_RULES.map((rule): ToolRule => ({ ...rule, risk: inferToolRisk(rule.toolName) })),
    [],
  );

  const others = useMemo(() => {
    const names = [...new Set(allToolNames.map((name) => name.trim()).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b));
    return names
      .filter((toolName) => !RECOMMENDED_TOOL_NAMES.has(toolName))
      .map((toolName): ToolRule => ({
        toolName,
        label: toolName,
        risk: inferToolRisk(toolName),
        description: '后端返回的其他工具。默认需要手动审批。',
      }));
  }, [allToolNames]);

  return (
    <Card
      description="勾中的工具在这台浏览器的聊天页免审批，存在浏览器本地。QQ 和定时任务不看这里，它们只认上面那份服务端策略。"
      scope="this-browser"
      title="自动审批"
    >
      <div className="space-y-4">
        <div>
          <h3 className="mb-2 font-mono text-xs font-semibold text-[var(--duties-secondary)]">推荐工具</h3>
          <div className="space-y-2">
            {recommended.map((rule) => (
              <ToolRuleToggle
                checked={autoApproveTools[rule.toolName] ?? DEFAULT_AUTO_APPROVE_TOOLS[rule.toolName] ?? false}
                key={rule.toolName}
                onChange={(enabled) => setAutoApproveTool(rule.toolName, enabled)}
                rule={rule}
              />
            ))}
          </div>
        </div>

        {others.length > 0 && (
          <div>
            <h3 className="mb-2 font-mono text-xs font-semibold text-[var(--duties-secondary)]">其他工具</h3>
            <div className="space-y-2">
              {others.map((rule) => (
                <ToolRuleToggle
                  checked={autoApproveTools[rule.toolName] ?? false}
                  key={rule.toolName}
                  onChange={(enabled) => setAutoApproveTool(rule.toolName, enabled)}
                  rule={rule}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
};
