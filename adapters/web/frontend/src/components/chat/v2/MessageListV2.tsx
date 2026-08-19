// [2026-05-31] MessageListV2 renders only normalized WsMessage objects.
// [2026-06-04] Scroll fixes: track user scroll position, only auto-scroll when
// user is near bottom. Suppress scroll during initial history hydration.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type { ToolExecution, WsMessage } from '../../../types/message';
import { MessageCard } from './MessageCard';

interface MessageListV2Props {
  messages: WsMessage[];
  toolsById: Record<string, ToolExecution>;
}

// How far from the bottom (in px) the user can be and still count as "at bottom"
const SCROLL_THRESHOLD = 120;

function getScrollSignature(messages: WsMessage[], toolsById: Record<string, ToolExecution>): string {
  const messagePart = messages.map((message) => `${message.id}:${message.updatedAt}:${message.status}:${message.blocks.length}`).join('|');
  const toolPart = Object.values(toolsById).map((tool) => `${tool.stableId}:${tool.updatedAt}:${tool.status}`).join('|');
  return `${messagePart}::${toolPart}`;
}

export const MessageListV2 = ({ messages, toolsById }: MessageListV2Props) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  // [AutoC 2026-06-04] Why: entering a session with history should not force-scroll
  // to bottom. How: suppress auto-scroll until the first user interaction or the
  // first genuinely new message arrives after mount. Purpose: users see the top of
  // the conversation on entry, not an instant jump to the end.
  const [isUserNearBottom, setIsUserNearBottom] = useState(false);
  const [hasInitialized, setHasInitialized] = useState(false);
  const prevMessageCountRef = useRef(0);

  const scrollSignature = useMemo(() => getScrollSignature(messages, toolsById), [messages, toolsById]);

  // Track scroll position to determine if user is near bottom
  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setIsUserNearBottom(distanceFromBottom <= SCROLL_THRESHOLD);
    // Once user scrolls at all, mark as initialized
    if (!hasInitialized) setHasInitialized(true);
  }, [hasInitialized]);

  // On mount / messages change: determine if this is initial load vs new message
  useEffect(() => {
    const currentCount = messages.length;
    const prevCount = prevMessageCountRef.current;
    prevMessageCountRef.current = currentCount;

    // First render with messages = history hydration → scroll to bottom
    if (!hasInitialized && prevCount === 0 && currentCount > 0) {
      // History just loaded. Scroll to bottom so user sees latest messages.
      setHasInitialized(true);
      setIsUserNearBottom(true);
      bottomRef.current?.scrollIntoView({ behavior: 'instant' });
      return;
    }

    // New message added (not initial load) and user is near bottom → scroll
    if (isUserNearBottom && currentCount > prevCount) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
      return;
    }

    // Streaming updates (same message count, signature changed) → only scroll if near bottom
    if (isUserNearBottom) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollSignature]);

  if (messages.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-sm text-[var(--duties-tertiary)]">
        请选择或创建一个对话。
      </div>
    );
  }

  return (
    <div ref={containerRef} className="h-full overflow-y-auto" onScroll={handleScroll}>
      <div className="mx-auto max-w-3xl">
        {messages.map((message) => (
          <MessageCard key={message.id} message={message} toolsById={toolsById} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
};

export type { MessageListV2Props };
