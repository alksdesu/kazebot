// [2026-05-31] ToolCallCard renders normalized tool executions for MessageCard v2.
// Why: the old ToolCallRow only understood finished legacy tool calls and could not
// show argument streaming, queued tools, async task starts, or hidden control tools.
// How: map every ToolStatus to compact styling, show one-line summaries, and expose
// structured arguments/results in a collapsible details panel. Purpose: keep tool
// progress visible inside the same message timeline that owns the text output.
import { useMemo, useState, type ReactNode } from 'react';

import { decideApproval } from '../../../api/supervisorClient';
import { shouldAutoApproveTool, useClientPrefsStore } from '../../../store/clientPrefsStore';
import type { ToolExecution, ToolStatus } from '../../../types/message';
import { Icon } from '../../common';

interface ToolCallCardProps {
  tool: ToolExecution;
}

interface StatusStyle {
  // [2026-06-01] Why: status icons used to be literal Unicode and emoji values.
  // How: store Material Symbol names instead and render them through Icon. Purpose:
  // the tool card remains data-driven while using one frontend icon system.
  icon: string;
  label: string;
  className: string;
  iconClassName: string;
  spin?: boolean;
}

interface DisplayText {
  text: string;
  sizeLabel: string;
}

interface ReadFileSection {
  path: string;
  content: string;
}

interface ListDirTree {
  directories: Record<string, unknown>[];
  totalFiles?: number;
  totalDirs?: number;
}

interface CommandResult {
  returnCode: number;
  output: string;
}

const DETAIL_PREVIEW_CHAR_LIMIT = 10000;
const PREVIEW_PRE_CLASS = 'max-h-56 overflow-auto whitespace-pre-wrap break-words bg-black/5 p-2';

const STATUS_STYLES: Record<ToolStatus, StatusStyle> = {
  args_streaming: {
    icon: 'progress_activity',
    label: '参数',
    className: 'border-blue-200 bg-blue-50 text-blue-700',
    iconClassName: 'text-blue-600',
    spin: true,
  },
  queued: {
    icon: 'pending',
    label: '排队中',
    className: 'border-[var(--duties-border)] bg-[var(--duties-bg)] text-[var(--duties-tertiary)]',
    iconClassName: 'text-[var(--duties-tertiary)]',
  },
  running: {
    icon: 'progress_activity',
    label: '运行中',
    className: 'border-blue-200 bg-blue-50 text-blue-700',
    iconClassName: 'text-blue-600',
    spin: true,
  },
  // [AutoC 2026-05-31] Why: approval is now a ToolExecution lifecycle state.
  // How: add an orange waiting style beside the existing running and terminal
  // states. Purpose: ToolCallCard can show that the tool is paused for a user
  // decision without creating a second approval card.
  awaiting_approval: {
    icon: 'verified_user',
    label: '需要审批',
    className: 'border-orange-200 bg-orange-50 text-orange-700',
    iconClassName: 'text-orange-600',
  },
  async_started: {
    icon: 'open_in_new',
    label: '异步已开始',
    className: 'border-indigo-200 bg-indigo-50 text-indigo-700',
    iconClassName: 'text-indigo-600',
  },
  success: {
    icon: 'check_circle',
    label: '成功',
    className: 'border-green-200 bg-green-50 text-green-700',
    iconClassName: 'text-green-600',
  },
  error: {
    icon: 'error',
    label: '错误',
    className: 'border-red-200 bg-red-50 text-red-700',
    iconClassName: 'text-red-600',
  },
  cancelled: {
    icon: 'cancel',
    label: '已取消',
    className: 'border-gray-200 bg-gray-50 text-gray-600',
    iconClassName: 'text-gray-500',
  },
};

function getByteLength(value: string): number {
  // Why: expanded tool details now preview large payloads instead of hard-truncating
  // them. How: calculate UTF-8 bytes from the original full string. Purpose: users can
  // see the true payload size even when the visible preview is capped at 10000 chars.
  if (typeof TextEncoder !== 'undefined') {
    return new TextEncoder().encode(value).length;
  }
  return value.length;
}

function formatByteSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function getPreviewText(text: string, showFull: boolean): string {
  const chars = Array.from(text);
  if (showFull || chars.length <= DETAIL_PREVIEW_CHAR_LIMIT) return text;

  // Why: rendering arbitrarily large details can freeze the chat timeline. How: show
  // a 10000-character preview and let the user opt in to the full payload. Purpose:
  // remove the old irreversible 2000-character truncation without losing performance.
  return chars.slice(0, DETAIL_PREVIEW_CHAR_LIMIT).join('');
}

function isPreviewLimited(text: string): boolean {
  return Array.from(text).length > DETAIL_PREVIEW_CHAR_LIMIT;
}

function createDisplayText(text: string): DisplayText | null {
  if (!text) return null;
  return {
    text,
    sizeLabel: `[${formatByteSize(getByteLength(text))}]`,
  };
}

function stringifyValue(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') return value;

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function getArgumentsText(tool: ToolExecution): string {
  if (tool.argumentsText) return tool.argumentsText;
  return stringifyValue(tool.arguments);
}

function getDataField(tool: ToolExecution): Record<string, unknown> | null {
  // Why: backend tool responses now use a unified { ok, data, error } envelope. How:
  // safely unwrap only object-shaped result.data values. Purpose: every renderer can
  // prefer current structured fields while preserving legacy raw_inline fallbacks.
  const result = tool.result;
  if (isRecord(result) && isRecord(result.data)) {
    return result.data;
  }
  return null;
}

function getLegacyResultRecord(tool: ToolExecution): Record<string, unknown> | null {
  // Why: older stored conversations may still have structure at result.* instead of
  // result.data.*. How: expose the top-level result object only when it is not the new
  // envelope. Purpose: keep historical tool cards readable after the backend change.
  if (isRecord(tool.result) && !isRecord(tool.result.data)) return tool.result;
  return null;
}

function getReadableDataResult(tool: ToolExecution): string {
  // Why: data.result is the backend-provided human-readable result string. How: return
  // it only when it is already text. Purpose: unknown tools show useful text instead
  // of dumping the full unified envelope as JSON.
  const data = getDataField(tool);
  const dataResult = data?.result;
  return typeof dataResult === 'string' ? dataResult : '';
}

function getRawResultText(tool: ToolExecution): string {
  return getReadableDataResult(tool) || stringifyValue(tool.rawInline || tool.result);
}

function getResultText(tool: ToolExecution): string {
  // Why: unified tool responses moved readable output from top-level result/raw_inline
  // into result.data.result. How: error remains first, then data.result, then legacy
  // raw_inline/result. Purpose: collapsed size labels and expanded fallback previews
  // match the text users should read for both new and old tool events.
  if (tool.error) return tool.error;
  const readableResult = getReadableDataResult(tool);
  if (readableResult) return readableResult;
  if (tool.rawInline) return tool.rawInline;
  return stringifyValue(tool.result);
}

function getCompactPreview(text: string): string {
  const compact = text.replace(/\s+/g, ' ').trim();
  if (compact.length <= 120) return compact;
  return `${compact.slice(0, 120)}…`;
}

function getStatusStyle(tool: ToolExecution): StatusStyle {
  if (tool.control && (tool.rejected || tool.status === 'error')) {
    return STATUS_STYLES.error;
  }

  return STATUS_STYLES[tool.status];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function getStringValue(value: unknown): string {
  return typeof value === 'string' ? value : value === undefined || value === null ? '' : String(value);
}

function getNumberValue(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function getBooleanValue(value: unknown): boolean {
  return value === true;
}

function getApprovalDetailsRecord(tool: ToolExecution): Record<string, unknown> {
  // [AutoC 2026-05-31] Why: approval metadata is stored on ToolExecution as an
  // untyped record so it can preserve backend payloads across versions. How: unwrap
  // approvalDetails.details only when it is object-shaped. Purpose: the inline
  // approval UI can display path and reason without duplicating ApprovalBlock.
  const nested = isRecord(tool.approvalDetails?.details) ? tool.approvalDetails.details : undefined;
  return nested || {};
}

function getApprovalOperation(tool: ToolExecution): string {
  // [AutoC 2026-05-31] Why: approvalDetails may come from old or new event shapes.
  // How: prefer the explicit operation field and fall back to the tool name. Purpose:
  // pending approval panels always show a useful operation label.
  const operation = isRecord(tool.approvalDetails) ? getStringValue(tool.approvalDetails.operation) : '';
  return operation || tool.name;
}

function getResultRecord(tool: ToolExecution): Record<string, unknown> | undefined {
  if (isRecord(tool.result)) return tool.result;
  if (!tool.rawInline) return undefined;

  try {
    const parsed = JSON.parse(tool.rawInline);
    return isRecord(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function parseReadFileData(tool: ToolExecution): ReadFileSection[] {
  const data = getDataField(tool) || getLegacyResultRecord(tool);
  const rows = Array.isArray(data?.results) ? data.results.filter(isRecord) : [];

  // Why: read_file now returns file entries as result.data.results instead of only a
  // printable transcript. How: convert object entries with path/content fields into
  // the same section shape used by the legacy parser. Purpose: structured new results
  // render as separate files and old raw_inline transcripts still work below.
  return rows
    .map((row, index) => ({
      path: getStringValue(row.path) || `文件-${index + 1}`,
      content: getStringValue(row.content),
    }))
    .filter((section) => section.path || section.content);
}

function parseReadFileSections(rawText: string): ReadFileSection[] {
  const matches = Array.from(rawText.matchAll(/^── (.*?) ──(?:[ \t]*(.*))?$/gm));
  if (matches.length === 0) return [];

  // Why: read_file returns a compact text transcript containing one or more file
  // headers. How: split on the backend header line and keep each header path beside
  // its own content. Purpose: expanded details read like files instead of one dump.
  return matches.map((match, index) => {
    const headerEnd = (match.index || 0) + match[0].length;
    const nextStart = matches[index + 1]?.index ?? rawText.length;
    const suffix = (match[2] || '').trim();
    const body = rawText.slice(headerEnd, nextStart).replace(/^\r?\n/, '').replace(/\r?\n$/, '');
    return {
      path: match[1] || '文件',
      content: suffix ? `${suffix}${body ? `\n${body}` : ''}` : body,
    };
  });
}

function parseCommandResult(tool: ToolExecution): CommandResult | null {
  const data = getDataField(tool) || getLegacyResultRecord(tool);
  const returnCode = getNumberValue(data?.returncode);
  if (returnCode !== undefined) {
    // Why: execute_command and remote_exec now expose status and transcript as
    // result.data.returncode/result.data.output. How: read those fields first, with
    // legacy top-level result support through getLegacyResultRecord. Purpose: users
    // see accurate status even when raw_inline is absent or stale.
    return {
      returnCode,
      output: getStringValue(data?.output),
    };
  }

  const rawText = stringifyValue(tool.rawInline || tool.result);
  const match = rawText.match(/^returncode=(-?\d+)\r?\n?([\s\S]*)$/);
  if (!match) return null;

  // Why: execute_command legacy events include machine-readable status in raw text.
  // How: extract returncode before rendering output. Purpose: historical transcripts
  // keep their compact status badge after the unified result migration.
  return {
    returnCode: Number(match[1]),
    output: match[2] || '',
  };
}

function parseSearchResults(tool: ToolExecution): { rows: Record<string, unknown>[]; count: number; truncated: boolean } | null {
  const data = getDataField(tool) || getLegacyResultRecord(tool);
  if (!data) return null;

  // Why: search_in_files moved rows and counters under result.data. How: normalize
  // data.results, data.count, and data.truncated with legacy top-level fallback.
  // Purpose: the search renderer shows a concise hit list instead of an envelope dump.
  const rows = Array.isArray(data.results) ? data.results.filter(isRecord) : [];
  return {
    rows,
    count: getNumberValue(data.count) ?? rows.length,
    truncated: getBooleanValue(data.truncated),
  };
}

function parseListDirTree(tool: ToolExecution): ListDirTree | null {
  const data = getDataField(tool) || getLegacyResultRecord(tool);
  if (!data) return null;

  // Why: list_dir now returns directory listings and totals under result.data. How:
  // normalize the directory array and optional totals from data first, then support old
  // top-level result fields. Purpose: directory trees remain structured across formats.
  return {
    directories: Array.isArray(data.results) ? data.results.filter(isRecord) : [],
    totalFiles: getNumberValue(data.totalFiles),
    totalDirs: getNumberValue(data.totalDirs),
  };
}

function renderJsonWithHighlightedKeys(text: string): ReactNode {
  const nodes: ReactNode[] = [];
  const pattern = /("(?:\\.|[^"\\])*")(\s*:)?/g;
  let lastIndex = 0;
  let tokenIndex = 0;
  let match: RegExpExecArray | null;

  // Why: unknown JSON tools still need a readable fallback. How: wrap only object keys
  // with a blue span while preserving the original whitespace in a pre block. Purpose:
  // provide lightweight syntax coloring without adding a dependency.
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }

    if (match[2]) {
      nodes.push(
        <span key={`json-key-${tokenIndex}`} className="text-blue-600">
          {match[1]}
        </span>,
      );
      nodes.push(match[2]);
    } else {
      nodes.push(match[0]);
    }

    lastIndex = match.index + match[0].length;
    tokenIndex += 1;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
}

interface LayeredPreviewProps {
  text: string;
  emptyLabel?: string;
  renderText?: (visibleText: string) => ReactNode;
}

function LayeredPreview({ text, emptyLabel = '', renderText }: LayeredPreviewProps) {
  const [showFull, setShowFull] = useState(false);
  const limited = isPreviewLimited(text);
  const visibleText = getPreviewText(text, showFull);
  const renderedText = visibleText || emptyLabel;

  return (
    <div className="space-y-1">
      {renderText ? renderText(renderedText) : <pre className={PREVIEW_PRE_CLASS}>{renderedText}</pre>}
      {limited && !showFull && (
        <button
          type="button"
          className="text-[0.64rem] font-semibold text-blue-600 hover:underline"
          onClick={() => setShowFull(true)}
        >
          查看完整内容
        </button>
      )}
    </div>
  );
}

function renderReadFileResult(tool: ToolExecution): ReactNode {
  const dataSections = parseReadFileData(tool);
  const rawText = stringifyValue(tool.rawInline || tool.result);
  const sections = dataSections.length > 0 ? dataSections : parseReadFileSections(rawText);

  if (sections.length === 0) {
    return <LayeredPreview text={getRawResultText(tool)} />;
  }

  return (
    <div className="space-y-2">
      {sections.map((section, index) => (
        <div key={`${section.path}-${index}`} className={index > 0 ? 'border-t border-current/10 pt-2' : ''}>
          <div className="mb-1 font-semibold text-[var(--duties-text)]">{section.path}</div>
          <LayeredPreview text={section.content} emptyLabel="（空文件）" />
        </div>
      ))}
    </div>
  );
}

function renderExecuteCommandResult(tool: ToolExecution): ReactNode {
  const commandResult = parseCommandResult(tool);

  if (!commandResult) {
    return <LayeredPreview text={getRawResultText(tool)} />;
  }

  const badgeClassName = commandResult.returnCode === 0
    ? 'border-green-200 bg-green-100 text-green-700'
    : 'border-red-200 bg-red-100 text-red-700';

  return (
    <div className="space-y-2">
      <span className={`inline-flex rounded border px-1.5 py-0.5 font-semibold ${badgeClassName}`}>
        返回码={commandResult.returnCode}
      </span>
      <LayeredPreview text={commandResult.output} emptyLabel="（无输出）" />
    </div>
  );
}

function renderSearchInFilesResult(tool: ToolExecution): ReactNode {
  const parsed = parseSearchResults(tool);

  if (!parsed) {
    return renderFallbackResult(tool);
  }

  const { rows, count, truncated } = parsed;

  return (
    <div className="space-y-1">
      <div className="font-semibold text-[var(--duties-text)]">
        找到 {count} 个结果 {truncated && <span className="font-normal text-[var(--duties-tertiary)]">（结果已截断）</span>}
      </div>
      <div className="space-y-1">
        {rows.map((row, index) => {
          const file = getStringValue(row.file) || '（未知文件）';
          const line = (getNumberValue(row.line) ?? getStringValue(row.line)) || '?';
          const match = getStringValue(row.match);
          const context = getStringValue(row.context);
          return (
            <div key={`${file}-${line}-${index}`} className="break-words rounded bg-black/5 px-2 py-1">
              <span className="font-semibold text-[var(--duties-text)]">{file}:{line}</span>
              <span className="text-[var(--duties-tertiary)]"> | </span>
              <span>{match}</span>
              {context && (
                <>
                  <span className="text-[var(--duties-tertiary)]"> | </span>
                  <span className="text-[var(--duties-tertiary)]">{context}</span>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function renderListDirResult(tool: ToolExecution): ReactNode {
  const parsed = parseListDirTree(tool);

  if (!parsed) {
    return renderFallbackResult(tool);
  }

  const { directories, totalFiles, totalDirs } = parsed;

  return (
    <div className="space-y-2">
      {(totalFiles !== undefined || totalDirs !== undefined) && (
        <div className="font-semibold text-[var(--duties-text)]">
          {totalDirs ?? 0} 个目录，{totalFiles ?? 0} 个文件
        </div>
      )}
      {directories.map((directory, directoryIndex) => {
        const path = getStringValue(directory.path) || '.';
        const entries = Array.isArray(directory.entries) ? directory.entries.filter(isRecord) : [];
        return (
          <div key={`${path}-${directoryIndex}`} className={directoryIndex > 0 ? 'border-t border-current/10 pt-2' : ''}>
            <div className="inline-flex items-center gap-1 font-semibold text-[var(--duties-text)]">
              {/* [2026-06-01] Why: directory result headers used folder emoji.
                  How: render folder through Material Symbols. Purpose: structured
                  list_dir previews match the rest of the tool icon migration. */}
              <Icon name="folder" size={14} />
              <span>{path}</span>
            </div>
            <div className="mt-1 space-y-0.5 pl-3">
              {entries.map((entry, entryIndex) => {
                const type = getStringValue(entry.type);
                const iconName = type === 'directory' ? 'folder' : 'draft';
                return (
                  <div key={`${path}-${entryIndex}`} className="inline-flex items-center gap-1">
                    {/* [2026-06-01] Why: list entries used folder and document emoji.
                        How: select a Material Symbol name from the entry type. Purpose:
                        generated directory previews no longer emit emoji. */}
                    <Icon name={iconName} size={13} />
                    <span>{getStringValue(entry.name)}</span>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function renderJsonResult(tool: ToolExecution): ReactNode {
  const readableResult = getReadableDataResult(tool);
  if (readableResult) {
    // Why: unknown JSON tools now often include a human-readable result.data.result.
    // How: render that string before syntax-highlighting the full envelope. Purpose:
    // users get the intended text fallback instead of a less readable JSON dump.
    return <LayeredPreview text={readableResult} />;
  }

  const jsonSource = tool.result !== undefined ? tool.result : getResultRecord(tool) || tool.rawInline;
  const jsonText = stringifyValue(jsonSource);

  return (
    <LayeredPreview
      text={jsonText}
      renderText={(visibleText) => (
        <pre className={PREVIEW_PRE_CLASS}>{renderJsonWithHighlightedKeys(visibleText)}</pre>
      )}
    />
  );
}

function renderFallbackResult(tool: ToolExecution): ReactNode {
  // Why: generic tools can now provide readable text in result.data.result even when
  // their format is json. How: getResultText checks data.result before legacy raw text
  // and JSON serialization. Purpose: unknown unified tools render the clearest text.
  return <LayeredPreview text={getResultText(tool)} />;
}

function renderResult(tool: ToolExecution): ReactNode {
  if (tool.status === 'error' || tool.rejected) {
    return <LayeredPreview text={getResultText(tool)} />;
  }

  // Why: unified tool envelopes make structured fields available by tool name even
  // when format metadata is missing or no longer matches the old raw_inline style.
  // How: dispatch known tools by name first, then fall back to plain data.result text
  // or highlighted JSON. Purpose: current and historical tool cards both stay readable.
  if (tool.name === 'read_file') {
    return renderReadFileResult(tool);
  }
  if (tool.name === 'execute_command' || tool.name === 'remote_exec') {
    return renderExecuteCommandResult(tool);
  }
  if (tool.name === 'search_in_files') {
    return renderSearchInFilesResult(tool);
  }
  if (tool.name === 'list_dir') {
    return renderListDirResult(tool);
  }
  if (tool.format === 'json') {
    return renderJsonResult(tool);
  }

  return renderFallbackResult(tool);
}

export const ToolCallCard = ({ tool }: ToolCallCardProps) => {
  const toolResultsDefaultCollapsed = useClientPrefsStore(state => state.toolResultsDefaultCollapsed);
  const autoApproveTools = useClientPrefsStore(state => state.autoApproveTools);
  // [2026-06-01] Tool detail expansion now follows clientPrefsStore.
  // Why: result disclosure was hard-coded as collapsed. How: initialize the local
  // disclosure state from the browser preference. Purpose: each frontend can choose
  // whether tool details start open without changing backend output.
  const [expanded, setExpanded] = useState(() => !toolResultsDefaultCollapsed);
  // [AutoC 2026-05-31] Why: approval decisions are now submitted from inside the
  // tool card. How: keep a local loading flag only for the clicked network request
  // and let WebSocket replay update approvalStatus. Purpose: the UI avoids duplicate
  // clicks without inventing a second source of truth for approval state.
  const [approvalLoading, setApprovalLoading] = useState(false);
  const [approvalError, setApprovalError] = useState('');
  const statusStyle = getStatusStyle(tool);
  const argumentsText = useMemo(() => getArgumentsText(tool), [tool]);
  const resultText = useMemo(() => getResultText(tool), [tool]);
  const argumentDisplay = useMemo(() => createDisplayText(argumentsText), [argumentsText]);
  const resultDisplay = useMemo(() => createDisplayText(resultText), [resultText]);
  const collapsedSummary = tool.summary
    ? getCompactPreview(tool.summary)
    : tool.status === 'args_streaming'
      ? getCompactPreview(argumentDisplay?.text || '')
      : '';
  const approvalDetails = getApprovalDetailsRecord(tool);
  const approvalOperation = getApprovalOperation(tool);
  const canExpand = Boolean(argumentDisplay || resultDisplay || tool.taskId || tool.nodeId || tool.nodeName || tool.approvalDetails);
  const isAutoApprovedPending = tool.status === 'awaiting_approval'
    && tool.approvalStatus === 'pending'
    && shouldAutoApproveTool(tool.name, autoApproveTools);

  const handleApproval = async (approvalId: string, decision: 'allow' | 'deny') => {
    // [AutoC 2026-05-31] Why: the same tool card now owns the approval action.
    // How: call the existing supervisor client and rely on approval_decided events
    // to update the reducer. Purpose: the button path stays compatible with the
    // old ApprovalCard API while rendering from ToolExecution.
    setApprovalLoading(true);
    setApprovalError('');
    try {
      await decideApproval(approvalId, decision, `${decision} via tool card`);
    } catch (error) {
      setApprovalError(error instanceof Error ? error.message : '提交审批决定失败。');
    } finally {
      setApprovalLoading(false);
    }
  };

  if (tool.hidden) {
    return null;
  }

  return (
    <div className={`overflow-hidden border text-[0.72rem] ${statusStyle.className}`}>
      <button
        type="button"
        className={`flex w-full items-start gap-2 px-2.5 py-2 text-left font-mono ${canExpand ? 'cursor-pointer' : 'cursor-default'}`}
        aria-expanded={canExpand ? expanded : undefined}
        onClick={() => canExpand && setExpanded((value) => !value)}
      >
        <span className={`mt-0.5 w-3 flex-shrink-0 text-[0.65rem] text-[var(--duties-tertiary)] ${expanded ? 'rotate-90' : ''}`}>
          {/* [2026-06-01] Why: tool cards used triangle disclosure glyphs.
              How: render chevron_right through Material Symbols and rotate it when
              expanded. Purpose: the v2 tool UI no longer emits decorative Unicode. */}
          {canExpand ? <Icon name="chevron_right" size={13} /> : null}
        </span>
        <span className={`mt-0.5 w-4 flex-shrink-0 font-semibold ${statusStyle.iconClassName} ${statusStyle.spin ? 'inline-block animate-spin' : ''}`}>
          <Icon name={statusStyle.icon} size={13} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="font-semibold text-[var(--duties-text)]">{tool.name}</span>
          <span className="ml-2 text-[var(--duties-tertiary)]">{statusStyle.label}</span>
          {tool.elapsedMs !== undefined && (
            <span className="ml-2 text-[var(--duties-tertiary)]">{Math.round(tool.elapsedMs)}ms</span>
          )}
          {collapsedSummary && <span className="ml-2 break-words text-[var(--duties-secondary)]">{collapsedSummary}</span>}
        </span>
      </button>

      {tool.status === 'awaiting_approval' && tool.approvalId && (
        <div className="space-y-2 border-t border-current/10 bg-white/45 px-3 py-2 font-mono text-[0.66rem]">
          <div className="space-y-1 text-[var(--duties-secondary)]">
            <div><span className="text-[var(--duties-tertiary)]">操作：</span> <code className="text-[var(--duties-text)]">{approvalOperation}</code></div>
            {approvalDetails.path !== undefined && (
              <div><span className="text-[var(--duties-tertiary)]">路径：</span> <code className="text-[var(--duties-text)]">{getStringValue(approvalDetails.path)}</code></div>
            )}
            {approvalDetails.reason !== undefined && (
              <div><span className="text-[var(--duties-tertiary)]">原因：</span> {getStringValue(approvalDetails.reason)}</div>
            )}
          </div>
          {isAutoApprovedPending ? (
            <div className="inline-flex items-center gap-1 rounded-sm bg-gray-100 px-2 py-1 text-xs font-semibold text-gray-600">
              {/* [2026-06-01] Why: auto-approved tools should not present manual
                  buttons while the local client decision is being submitted. How:
                  show a muted badge instead. Purpose: users can distinguish local
                  auto-approval from a pending manual decision. */}
              <Icon name="check_circle" size={14} />
              <span>已自动放行</span>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="inline-flex items-center gap-1 rounded-sm bg-green-50 px-2 py-1 text-xs font-medium text-green-700 hover:bg-green-100 disabled:cursor-not-allowed disabled:opacity-60"
                onClick={() => handleApproval(tool.approvalId!, 'allow')}
                disabled={approvalLoading}
              >
                {/* [2026-06-01] Why: approval buttons used emoji status marks.
                    How: render Material Symbols before each label. Purpose: the inline
                    approval flow uses the same icon font as status rows. */}
                <Icon name="check_circle" size={14} />
                <span>允许</span>
              </button>
              <button
                type="button"
                className="inline-flex items-center gap-1 rounded-sm bg-red-50 px-2 py-1 text-xs font-medium text-red-600 hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-60"
                onClick={() => handleApproval(tool.approvalId!, 'deny')}
                disabled={approvalLoading}
              >
                <Icon name="cancel" size={14} />
                <span>拒绝</span>
              </button>
              {approvalLoading && <span className="text-[var(--duties-tertiary)]">提交中…</span>}
            </div>
          )}
          {approvalError && <div className="text-xs font-semibold text-red-600">{approvalError}</div>}
        </div>
      )}

      {tool.approvalStatus === 'allowed' && (
        <div className="inline-flex items-center gap-1 border-t border-current/10 bg-white/45 px-3 py-2 font-mono text-xs font-semibold text-green-600">
          {/* [2026-06-01] Why: approval result banners used emoji marks.
              How: render Material Symbols beside the result text. Purpose: post-
              decision states do not reintroduce platform emoji. */}
          <Icon name="check_circle" size={14} />
          <span>已批准</span>
        </div>
      )}

      {tool.approvalStatus === 'denied' && (
        <div className="inline-flex items-center gap-1 border-t border-current/10 bg-white/45 px-3 py-2 font-mono text-xs font-semibold text-red-500">
          <Icon name="cancel" size={14} />
          <span>已拒绝</span>
        </div>
      )}

      {expanded && canExpand && (
        <div className="space-y-2 border-t border-current/10 bg-white/50 px-3 py-2 font-mono text-[0.66rem] text-[var(--duties-secondary)]">
          {(tool.taskId || tool.nodeId || tool.nodeName) && (
            <div className="space-y-0.5">
              <div className="font-semibold text-[var(--duties-text)]">执行信息</div>
              {tool.taskId && <div>任务：<code>{tool.taskId}</code></div>}
              {tool.nodeName && <div>节点：<code>{tool.nodeName}</code></div>}
              {tool.nodeId && <div>节点 ID：<code>{tool.nodeId}</code></div>}
            </div>
          )}
          {argumentDisplay && (
            <div>
              <div className="mb-1 font-semibold text-[var(--duties-text)]">
                参数 <span className="font-normal text-[var(--duties-tertiary)]">{argumentDisplay.sizeLabel}</span>
              </div>
              <LayeredPreview text={argumentDisplay.text} />
            </div>
          )}
          {resultDisplay && (
            <div>
              <div className={`mb-1 font-semibold ${tool.status === 'error' || tool.rejected ? 'text-red-600' : 'text-[var(--duties-text)]'}`}>
                {tool.status === 'error' || tool.rejected ? '错误' : '结果'} <span className="font-normal text-[var(--duties-tertiary)]">{resultDisplay.sizeLabel}</span>
              </div>
              {renderResult(tool)}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export type { ToolCallCardProps };
