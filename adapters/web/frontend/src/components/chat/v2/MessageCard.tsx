// [2026-05-31] MessageCard is the unified v2 renderer for normalized chat messages.
// Why: Step 2B replaces the old MessageBubble plus StreamPreview split with one replayable
// message surface. How: derive header, role styling, streaming indicator, ordered block
// rendering, and attachments from WsMessage only. Purpose: make active and historical
// messages follow the same UI contract before the app is rewired to v2.
import type { Attachment, MessageRole, MessageStatus, TextBlock, ToolExecution, WsMessage } from '../../../types/message';
import { useChatStore } from '../../../store/chatStore';
import { Icon } from '../../common';
import { RenderBlockView } from './RenderBlockView';

interface MessageCardProps {
  message: WsMessage;
  toolsById: Record<string, ToolExecution>;
}

const ROLE_LABELS: Record<Exclude<MessageRole, 'dispatch_callback'>, string> = {
  user: '你',
  assistant: '助手',
  system: '系统',
};

const ROLE_STYLES: Record<MessageRole, { row: string; label: string }> = {
  user: {
    row: 'bg-[var(--duties-bg)]',
    label: 'text-[var(--duties-text)]',
  },
  assistant: {
    row: 'bg-[var(--duties-panel)]',
    label: 'text-blue-600',
  },
  system: {
    row: 'bg-orange-50/60',
    label: 'text-orange-600',
  },
  // [AutoC 2026-06-03] Why: dispatch callbacks need a visual lane separate from
  // user, assistant, and system cards. How: apply a subtle purple row and label.
  // Purpose: the callback remains fully expanded while being easy to scan.
  dispatch_callback: {
    row: 'bg-purple-50/40',
    label: 'text-purple-600',
  },
};

const STATUS_LABELS: Record<MessageStatus, string> = {
  pending: '等待中',
  streaming: '输出中',
  running_tools: '工具运行中',
  awaiting_approval: '等待审批',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

function isActiveStatus(status: MessageStatus): boolean {
  return status === 'streaming' || status === 'running_tools';
}

function getDispatchCallbackTitle(message: WsMessage): string {
  // [AutoC 2026-06-04] Why: dispatch_result payload text is now raw child output and
  // no longer includes a localized title. How: derive the Chinese title from
  // MessageSource caller/child fields, with the old fixed label only when all
  // structured fields are absent. Purpose: old history remains readable while new
  // cards never parse result text for presentation.
  const source = message.source || {};
  const nodeId = (source.childNodeId || source.nodeId || '').trim();
  const callerNodeId = (source.callerNodeId || '').trim();
  const hasStructuredSource = Boolean(nodeId || callerNodeId || source.summary || source.childSessionId || source.childTaskId);
  if (!hasStructuredSource) return '子节点回调';
  const displayNodeId = nodeId || '未知';
  return callerNodeId ? `${callerNodeId} 委派的 ${displayNodeId} 已完成` : `子节点 ${displayNodeId} 已完成`;
}

function getRoleLabel(message: WsMessage): string {
  if (message.role === 'dispatch_callback') return getDispatchCallbackTitle(message);
  return ROLE_LABELS[message.role];
}

function getBlocksContainerClassName(message: WsMessage): string {
  // [2026-06-02] Why: reply and finish should share the left-border pattern while
  // user messages must remain unbordered even when their text delivery is final. How:
  // compute the border at the MessageCard container from role plus completionType.
  // Purpose: live and hydrated assistant reply/finish cards are visually distinct
  // without relying on block-level delivery styling.
  if (message.role !== 'assistant') return 'space-y-2';
  if (message.completionType === 'reply') return 'space-y-2 border-l-2 border-blue-400 pl-3';
  if (message.completionType === 'finish' && message.status !== 'failed') return 'space-y-2 border-l-2 border-green-400 pl-3';
  return 'space-y-2';
}

function getStatusClassName(status: MessageStatus): string {
  if (status === 'failed') return 'text-red-600';
  if (status === 'cancelled') return 'text-gray-500';
  if (status === 'awaiting_approval') return 'text-orange-600';
  if (isActiveStatus(status)) return 'text-blue-600';
  return 'text-[var(--duties-tertiary)]';
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date);
}

function getAttachmentHref(attachment: Attachment): string | undefined {
  if (attachment.path) return `/${attachment.path}`;
  return attachment.url;
}

function isImageAttachment(attachment: Attachment): boolean {
  return attachment.type === 'image' || Boolean(attachment.mime_type?.startsWith('image/'));
}

function formatAttachmentMeta(attachment: Attachment): string {
  if (!attachment.size) return attachment.name;
  const kb = attachment.size / 1024;
  const size = kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(kb))} KB`;
  return `${attachment.name} (${size})`;
}

export const MessageCard = ({ message, toolsById }: MessageCardProps) => {
  const roleStyle = ROLE_STYLES[message.role];
  const active = isActiveStatus(message.status);
  const blocksContainerClassName = getBlocksContainerClassName(message);
  const attachments = message.attachments ?? [];
  // [AutoC 2026-06-03] Why: only dispatch callback cards can navigate to child
  // sessions. How: read the backend-provided source.childSessionId and leave normal
  // messages without an action. Purpose: navigation stays structured and does not
  // parse the callback text.
  const childSessionId = message.role === 'dispatch_callback' ? message.source.childSessionId?.trim() || '' : '';
  // [AutoC 2026-06-04] Why: child result summaries moved out of backend text into
  // structured source metadata. How: render source.summary directly below the dynamic
  // dispatch title. Purpose: users see the summary without duplicating it in payload.text.
  const dispatchSummary = message.role === 'dispatch_callback' ? message.source.summary?.trim() || '' : '';

  return (
    <article className={`border-b border-[var(--duties-border)] px-3 py-3 sm:px-4 ${roleStyle.row}`}>
      <div className="mx-auto max-w-3xl">
        <header className="mb-1.5 flex flex-wrap items-center gap-2">
          <span className={`font-mono text-[0.6rem] font-semibold uppercase tracking-[0.18em] ${roleStyle.label}`}>
            {getRoleLabel(message)}
          </span>
          <time className="font-mono text-[0.55rem] text-[var(--duties-tertiary)]" dateTime={message.createdAt}>
            {formatTime(message.createdAt)}
          </time>
          {/* [2026-06-02] Why: finish no longer uses a special green status pill.
              How: always render the normal status text and move finish emphasis to
              the assistant-only body border. Purpose: reply and finish have one
              consistent visual language while status remains plain text. */}
          <span className={`font-mono text-[0.55rem] ${getStatusClassName(message.status)}`}>{STATUS_LABELS[message.status]}</span>
          {message.source.nodeName && (
            <span className="font-mono text-[0.55rem] text-[var(--duties-tertiary)]">{message.source.nodeName}</span>
          )}
          {active && <span aria-label="消息正在活动" className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />}
        </header>

        {dispatchSummary && (
          <div className="mb-2 text-xs text-purple-700">
            {dispatchSummary}
          </div>
        )}

        <div className={blocksContainerClassName}>
          {message.blocks
            .filter((block) => {
              // [2026-06-03] Why: free prose (stream/final text) duplicates reply text
              // and clutters the card. How: when a card contains any intermediate text
              // block (from reply/ask), hide non-intermediate text blocks. Cards without
              // any intermediate block (pure free prose) render normally.
              // Purpose: reply/finish/ask text is the authoritative user-facing output;
              // free prose is internal LLM reasoning noise.
              if (block.kind !== 'text') return true;
              const hasIntermediateText = message.blocks.some(
                (b) => b.kind === 'text' && (b as TextBlock).delivery === 'intermediate',
              );
              if (!hasIntermediateText) return true;
              return (block as TextBlock).delivery === 'intermediate';
            })
            .map((block) => (
              <RenderBlockView key={block.id} block={block} toolsById={toolsById} />
            ))}
        </div>

        {childSessionId && (
          <button
            className="mt-1 text-xs font-mono text-purple-600 underline hover:text-purple-800"
            onClick={() => useChatStore.getState().viewChildSession(childSessionId)}
          >
            {/* [AutoC 2026-06-03] Why: dispatch_result payloads now carry the child
                session id. How: call the store's child-session view action directly
                with that id. Purpose: users can inspect the child node transcript
                without relying on the sidebar or text parsing. */}
            查看子节点详情 →
          </button>
        )}

        {attachments.length > 0 && (
          <footer className="mt-2 flex flex-wrap gap-2">
            {attachments.map((attachment, index) => {
              const href = getAttachmentHref(attachment);
              const key = `${attachment.name}-${index}`;

              if (isImageAttachment(attachment) && href) {
                return (
                  <a key={key} href={href} target="_blank" rel="noopener noreferrer" className="block">
                    <img src={href} alt={attachment.name} className="max-h-64 border border-[var(--duties-border)]" />
                  </a>
                );
              }

              return (
                <a
                  key={key}
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={`inline-flex items-center gap-1 border border-[var(--duties-border)] px-2 py-0.5 text-xs text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)] ${href ? '' : 'pointer-events-none opacity-70'}`}
                >
                  {/* [2026-06-01] Why: replace the attachment emoji with Material Symbols.
                      How: render attach_file before the attachment metadata. Purpose:
                      file links do not depend on platform emoji rendering. */}
                  <Icon name="attach_file" size={14} />
                  <span>{formatAttachmentMeta(attachment)}</span>
                </a>
              );
            })}
          </footer>
        )}
      </div>
    </article>
  );
};

export type { MessageCardProps };
