// Renders the visible conversation (or child session) as WsMessage cards, with history paging on top.
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';

import type { HistoryView } from '../../../store/chatStore';
import type { ToolExecution, WsMessage } from '../../../types/message';
import { Button } from '../../settings/pages/settingsControls';
import { MessageCard } from './MessageCard';

interface MessageListV2Props {
  messages: WsMessage[];
  toolsById: Record<string, ToolExecution>;
  history?: HistoryView;
  onLoadOlder?: () => void;
  onRetryHistory?: () => void;
}

// Within this many pixels of the bottom the reader counts as following the stream.
const SCROLL_THRESHOLD = 120;

function getScrollSignature(messages: readonly WsMessage[], toolsById: Readonly<Record<string, ToolExecution>>): string {
  // Only the tools these cards show: a tool streaming in another conversation must not move this list.
  return messages.map((message) => {
    const tools = message.blocks
      .flatMap((block) => (block.kind === 'tool' ? block.toolIds : []))
      .map((toolId) => toolsById[toolId])
      .filter((tool): tool is ToolExecution => Boolean(tool))
      .map((tool) => `${tool.stableId}:${tool.updatedAt}:${tool.status}`)
      .join(',');
    return `${message.id}:${message.updatedAt}:${message.status}:${message.blocks.length}:${tools}`;
  }).join('|');
}

const HistoryBar = ({ history, onLoadOlder, onRetryHistory }: Pick<MessageListV2Props, 'history' | 'onLoadOlder' | 'onRetryHistory'>) => {
  if (!history) return null;
  if (history.error) {
    return (
      <div className="flex items-center justify-between gap-3 px-4 py-2 text-xs" role="alert">
        <span className="text-[var(--duties-danger)]">历史消息刷新失败：{history.error}</span>
        {onRetryHistory && <Button size="sm" tone="quiet" onClick={onRetryHistory}>重试</Button>}
      </div>
    );
  }
  if (history.olderError) {
    return (
      <div className="flex items-center justify-between gap-3 px-4 py-2 text-xs" role="alert">
        <span className="text-[var(--duties-danger)]">{history.olderError}</span>
        {onLoadOlder && <Button size="sm" tone="quiet" onClick={onLoadOlder}>重试</Button>}
      </div>
    );
  }
  if (!history.hasMore && !history.loadingOlder) return null;
  return (
    <div className="flex justify-center px-4 py-2">
      <Button size="sm" tone="quiet" loading={history.loadingOlder} disabled={!onLoadOlder} onClick={onLoadOlder}>
        加载更早的消息
      </Button>
    </div>
  );
};

// The child-node panel shows the last list that was read; without this line a failed read looks like "no children".
const ChildrenNotice = ({ history, onRetryHistory }: Pick<MessageListV2Props, 'history' | 'onRetryHistory'>) => {
  if (!history?.childrenError) return null;
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-2 text-xs" role="alert">
      <span className="text-[var(--duties-danger)]">子节点列表没读到：{history.childrenError}</span>
      {onRetryHistory && <Button size="sm" tone="quiet" onClick={onRetryHistory}>重试</Button>}
    </div>
  );
};

// Approvals requested while the stream was down only reach the chat through this read; without the line a failed read looks like "nothing to approve".
const ApprovalsNotice = ({ history, onRetryHistory }: Pick<MessageListV2Props, 'history' | 'onRetryHistory'>) => {
  if (!history?.approvalsError) return null;
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-2 text-xs" role="alert">
      <span className="text-[var(--duties-danger)]">待审批没读到，断线期间要的审批可能没显示在这里：{history.approvalsError}</span>
      {onRetryHistory && <Button size="sm" tone="quiet" onClick={onRetryHistory}>重读</Button>}
    </div>
  );
};

const EmptyState = ({ history, onRetryHistory }: Pick<MessageListV2Props, 'history' | 'onRetryHistory'>) => {
  if (history?.loading) {
    return <p className="text-sm text-[var(--duties-tertiary)]" role="status">正在加载历史消息…</p>;
  }
  if (history?.error) {
    return (
      <div className="flex flex-col items-center gap-2 text-sm" role="alert">
        <span className="text-[var(--duties-danger)]">历史消息加载失败：{history.error}</span>
        {onRetryHistory && <Button size="sm" tone="quiet" onClick={onRetryHistory}>重试</Button>}
      </div>
    );
  }
  return <p className="text-sm text-[var(--duties-tertiary)]">发送消息，开始对话。也可以添加文件并说明要处理什么。</p>;
};

export const MessageListV2 = ({ messages, toolsById, history, onLoadOlder, onRetryHistory }: MessageListV2Props) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  // Entering a conversation lands at its latest message once; after that the list only
  // follows new output while the reader stays near the bottom.
  const [isUserNearBottom, setIsUserNearBottom] = useState(false);
  const [hasInitialized, setHasInitialized] = useState(false);
  const prevMessageCountRef = useRef(0);
  const firstMessageIdRef = useRef<string | undefined>(undefined);
  const distanceFromBottomRef = useRef(0);

  const scrollSignature = useMemo(() => getScrollSignature(messages, toolsById), [messages, toolsById]);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    distanceFromBottomRef.current = distanceFromBottom;
    setIsUserNearBottom(distanceFromBottom <= SCROLL_THRESHOLD);
    if (!hasInitialized) setHasInitialized(true);
  }, [hasInitialized]);

  useLayoutEffect(() => {
    const el = containerRef.current;
    const currentCount = messages.length;
    const prevCount = prevMessageCountRef.current;
    const previousFirstId = firstMessageIdRef.current;
    prevMessageCountRef.current = currentCount;
    firstMessageIdRef.current = messages[0]?.id;
    const prepended = Boolean(previousFirstId)
      && messages[0]?.id !== previousFirstId
      && messages.some((message) => message.id === previousFirstId);

    if (prepended && el) {
      // Older history went in above: keep the cards the reader was looking at where they were.
      el.scrollTop = el.scrollHeight - el.clientHeight - distanceFromBottomRef.current;
    } else if (!hasInitialized && prevCount === 0 && currentCount > 0) {
      setHasInitialized(true);
      setIsUserNearBottom(true);
      bottomRef.current?.scrollIntoView?.({ behavior: 'instant' });
    } else if (isUserNearBottom) {
      bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' });
    }
    if (el) distanceFromBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollSignature]);

  if (messages.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
        <ChildrenNotice history={history} onRetryHistory={onRetryHistory} />
        <ApprovalsNotice history={history} onRetryHistory={onRetryHistory} />
        <EmptyState history={history} onRetryHistory={onRetryHistory} />
      </div>
    );
  }

  return (
    <div ref={containerRef} className="h-full overflow-y-auto" onScroll={handleScroll}>
      <div className="mx-auto max-w-3xl">
        <HistoryBar history={history} onLoadOlder={onLoadOlder} onRetryHistory={onRetryHistory} />
        <ChildrenNotice history={history} onRetryHistory={onRetryHistory} />
        <ApprovalsNotice history={history} onRetryHistory={onRetryHistory} />
        {messages.map((message) => (
          <MessageCard key={message.id} message={message} toolsById={toolsById} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
};

export type { MessageListV2Props };
