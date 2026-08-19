// [2026-05-17] MessageBubble: Lim-Code-style tool rows over Clonoth's flat history model.
import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import type { ChatMessage, ToolCall } from '../../types';
import { Icon } from '../common';
import { ApprovalCard } from './ApprovalCard';

interface MessageBubbleProps {
  message: ChatMessage;
}

const FINAL_TEXT_TOOL_NAMES = new Set(['finish', 'reply']);

function getToolKey(toolCall: ToolCall, index: number): string {
  return toolCall.id || `${toolCall.name}-${index}`;
}

function getStatusIconName(toolCall: ToolCall): string {
  // [2026-06-01] Why: legacy tool rows used check, cross, and spinner glyphs.
  // How: return Material Symbol names and let Icon render them. Purpose: the old
  // MessageBubble path stays visually aligned with the v2 tool card migration.
  if (toolCall.status === 'success') return 'check_circle';
  if (toolCall.status === 'error') return 'error';
  return 'progress_activity';
}

function getStatusClass(toolCall: ToolCall): string {
  if (toolCall.status === 'success') return 'border-green-200 bg-green-50 text-green-700';
  if (toolCall.status === 'error') return 'border-red-200 bg-red-50 text-red-700';
  return 'border-[var(--duties-border)] bg-[var(--duties-bg)] text-[var(--duties-tertiary)]';
}

function getIconClass(toolCall: ToolCall): string {
  if (toolCall.status === 'success') return 'text-green-600';
  if (toolCall.status === 'error') return 'text-red-600';
  return 'text-[var(--duties-tertiary)]';
}

function stringifyArguments(args?: Record<string, unknown>): string {
  if (!args || Object.keys(args).length === 0) return '';
  return JSON.stringify(args, null, 2);
}

interface ToolCallRowProps {
  toolCall: ToolCall;
  index: number;
  expanded: boolean;
  onToggle: (toolKey: string) => void;
}

const ToolCallRow = ({ toolCall, index, expanded, onToggle }: ToolCallRowProps) => {
  const toolKey = getToolKey(toolCall, index);
  const hasArguments = !!stringifyArguments(toolCall.arguments);
  const hasResult = !!toolCall.result;
  const canExpand = hasArguments || hasResult;
  const ariaLabel = [toolCall.name, toolCall.summary].filter(Boolean).join(' ');

  return (
    <div className={`overflow-hidden border text-[0.72rem] ${getStatusClass(toolCall)}`}>
      <button
        type="button"
        className={`flex w-full items-start gap-2 px-2.5 py-2 text-left font-mono ${canExpand ? 'cursor-pointer' : 'cursor-default'}`}
        aria-expanded={canExpand ? expanded : undefined}
        aria-label={ariaLabel}
        onClick={() => canExpand && onToggle(toolKey)}
      >
        <span className={`mt-0.5 w-3 flex-shrink-0 text-[0.65rem] text-[var(--duties-tertiary)] ${expanded ? 'rotate-90' : ''}`}>
          {/* [2026-06-01] Why: legacy tool rows used triangle disclosure glyphs.
              How: render chevron_right through Material Symbols and rotate it for the
              expanded state. Purpose: collapsible rows do not emit Unicode symbols. */}
          {canExpand ? <Icon name="chevron_right" size={13} /> : null}
        </span>
        <span className={`mt-0.5 w-4 flex-shrink-0 font-semibold ${getIconClass(toolCall)}`}>
          <Icon name={getStatusIconName(toolCall)} size={13} className={toolCall.status ? '' : 'inline-block animate-spin'} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="font-semibold text-[var(--duties-text)]">{toolCall.name}</span>
          {toolCall.summary && (
            <span className="ml-2 break-words text-[var(--duties-secondary)]">{toolCall.summary}</span>
          )}
        </span>
      </button>

      {expanded && canExpand && (
        <div className="border-t border-current/10 bg-white/50 px-3 py-2 font-mono text-[0.66rem] text-[var(--duties-secondary)]">
          {hasArguments && (
            <div className="mb-2">
              <div className="mb-1 font-semibold text-[var(--duties-text)]">参数</div>
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-sm bg-black/5 p-2">
                {stringifyArguments(toolCall.arguments)}
              </pre>
            </div>
          )}
          {hasResult && (
            <div>
              <div className={`mb-1 font-semibold ${toolCall.status === 'error' ? 'text-red-600' : 'text-[var(--duties-text)]'}`}>
                {toolCall.status === 'error' ? '错误' : '结果摘要'}
              </div>
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-sm bg-black/5 p-2">
                {toolCall.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export const MessageBubble = ({ message }: MessageBubbleProps) => {
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const [expandedToolIds, setExpandedToolIds] = useState<Set<string>>(new Set());

  const isUser = message.role === 'user';
  const isSystem = message.role === 'system';
  const toolCalls = message.toolCalls ?? [];

  // [2026-05-17] finish/reply are completion controls, not ordinary tools. Their
  // text argument is already displayed as message.content, so they become banners
  // instead of rows and cannot duplicate user-facing text.
  // finish/reply with successful results just mark task completion;
  // rejected/errored finish is shown as a normal tool row like any other tool.
  const finishCalls = toolCalls.filter(tc => tc.name === 'finish');
  const replyCalls = toolCalls.filter(tc => tc.name === 'reply');
  const hasFinishSuccess = finishCalls.some(tc => tc.status === 'success');
  const hasReply = message.isIntermediate || replyCalls.length > 0;
  // Show all tools, but hide successful finish/reply (their text is the message content)
  const visibleToolCalls = toolCalls.filter(tc => {
    if (tc.name === 'finish' && tc.status === 'success') return false;
    if (tc.name === 'reply' && tc.status === 'success') return false;
    return true;
  });

  const toggleTool = (toolKey: string) => {
    setExpandedToolIds(prev => {
      const next = new Set(prev);
      if (next.has(toolKey)) next.delete(toolKey);
      else next.add(toolKey);
      return next;
    });
  };

  return (
    <div className={`border-b border-[var(--duties-border)] px-3 py-3 sm:px-4 sm:py-4 ${
      isUser ? 'bg-[var(--duties-bg)]' : 'bg-[var(--duties-panel)]'
    }`}>
      <div className="mx-auto max-w-3xl">
        {/* Role label */}
        <div className="mb-1.5 flex items-center gap-2">
          <span className={`font-mono text-[0.6rem] font-semibold uppercase tracking-[0.18em] ${
            isUser ? 'text-[var(--duties-text)]' : isSystem ? 'text-orange-600' : 'text-blue-600'
          }`}>
            {isUser ? '你' : isSystem ? '系统' : '助手'}
          </span>
          {message.isIntermediate && (
            <span className="font-mono text-[0.55rem] text-[var(--duties-tertiary)]">（中间回复）</span>
          )}
          <span className="font-mono text-[0.55rem] text-[var(--duties-tertiary)]">
            {new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(message.createdAt))}
          </span>
        </div>

        {/* Thinking chain — keeps the previous collapsible behavior. */}
        {message.thinking && (
          <div className="mb-2">
            <button
              className="flex items-center gap-1.5 font-mono text-[0.65rem] text-[var(--duties-tertiary)] transition-colors hover:text-[var(--duties-text)]"
              onClick={() => setThinkingOpen(!thinkingOpen)}
              type="button"
            >
              <span className={`transition-transform ${thinkingOpen ? 'rotate-90' : ''}`}>
                {/* [2026-06-01] Why: the thinking disclosure used a triangle glyph.
                    How: render chevron_right through Icon. Purpose: legacy thinking
                    controls follow the Material Symbols migration. */}
                <Icon name="chevron_right" size={13} />
              </span>
              <span>思考（{Math.ceil(message.thinking.length / 4)} token）</span>
            </button>
            {thinkingOpen && (
              <pre className="mt-1.5 max-h-64 overflow-y-auto border-l-2 border-[var(--duties-border)] pl-3 font-mono text-[0.7rem] leading-relaxed text-[var(--duties-secondary)]">
                {message.thinking}
              </pre>
            )}
          </div>
        )}

        {/* Tool calls — each non-finish tool is now an independent Lim-Code-style row. */}
        {visibleToolCalls.length > 0 && (
          <div className="mb-2 space-y-1.5">
            {visibleToolCalls.map((tc, i) => {
              const toolKey = getToolKey(tc, i);
              return (
                <ToolCallRow
                  key={toolKey}
                  toolCall={tc}
                  index={i}
                  expanded={expandedToolIds.has(toolKey)}
                  onToggle={toggleTool}
                />
              );
            })}
          </div>
        )}

        {/* Task Complete — subtle divider, not a big banner */}
        {hasFinishSuccess && (
          <div className="mb-2 flex items-center gap-2 text-[var(--duties-tertiary)]">
            <div className="h-px flex-1 bg-green-200" />
            <span className="inline-flex items-center gap-1 font-mono text-[0.6rem] text-green-600">
              {/* [2026-06-01] Why: replace the task completion checkmark with Material Symbols.
                  How: render check_circle before the existing label. Purpose: legacy
                  message completion UI no longer emits decorative Unicode. */}
              <Icon name="check_circle" size={12} />
              <span>任务完成</span>
            </span>
            <div className="h-px flex-1 bg-green-200" />
          </div>
        )}

        {/* Approval card */}
        {message.approval ? (
          <ApprovalCard approval={message.approval} />
        ) : message.content ? (
          /* Main content */
          <div className="markdown-body text-sm leading-6 text-[var(--duties-text)]">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        ) : null}

        {/* Attachments */}
        {message.attachments && message.attachments.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {message.attachments.map((att, i) => {
              const isImage = att.type === 'image' || att.mime_type?.startsWith('image/');
              const href = att.path ? `/${att.path}` : att.url;
              if (isImage && href) {
                return <img key={i} src={href} alt={att.name} className="max-h-64 border border-[var(--duties-border)]" />;
              }
              return (
                <a
                  key={i}
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 border border-[var(--duties-border)] px-2 py-0.5 text-xs text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)]"
                >
                  {/* [2026-06-01] Why: replace the attachment emoji with Material Symbols.
                      How: render attach_file before the file name. Purpose: attachment
                      rows share the same icon font as message badges. */}
                  <Icon name="attach_file" size={14} />
                  <span>{att.name}</span>
                </a>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
};
