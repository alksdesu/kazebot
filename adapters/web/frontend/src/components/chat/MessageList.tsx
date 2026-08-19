// [2026-05-16] Updated: StreamPreview integration, auto-scroll.
import { useEffect, useRef } from 'react';

import type { Conversation, StreamPreviewState } from '../../types';
import { MessageBubble } from './MessageBubble';
import { StreamPreview } from './StreamPreview';

interface MessageListProps {
  conversation: Conversation | null;
  isTyping: boolean;
  streamPreview: StreamPreviewState;
}

export const MessageList = ({ conversation, isTyping, streamPreview }: MessageListProps) => {
  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom on new messages or preview updates
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [conversation?.messages.length, streamPreview.textPreview, streamPreview.thinkingPreview, streamPreview.progressLines.length]);

  if (!conversation) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-sm text-[var(--duties-tertiary)]">
        请选择或创建一个对话。
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl">
        {conversation.messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
        {/* Stream preview — shown while generating */}
        {isTyping && <StreamPreview preview={streamPreview} />}
        <div ref={bottomRef} />
      </div>
    </div>
  );
};
