// [2026-05-31] TextBlockView renders normalized text blocks for MessageCard v2.
// Why: the new reducer stores final, intermediate, history, and streaming text in
// RenderBlock objects instead of the old split message/preview fields. How: render
// Markdown through ReactMarkdown and add small visual markers only from the block's
// delivery metadata. Purpose: keep all text output in the unified MessageCard path.
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import type { TextBlock } from '../../../types/message';

interface TextBlockViewProps {
  block: TextBlock;
}

function getDeliveryClassName(_delivery: TextBlock['delivery']): string {
  // [2026-06-02] Why: delivery is not role-aware, and user messages can also carry
  // final delivery. How: leave TextBlockView visually neutral and let MessageCard add
  // assistant-only reply or finish borders from message completionType. Purpose: user
  // text blocks never receive reply/finish borders by accident.
  return '';
}

// Strip protocol markers (<<<TOOL_CALL>>> ... <<<END_TOOL_CALL>>>) from stream text
// that may leak through before the tool parser consumes them.
const TOOL_CALL_PATTERN = /<<<TOOL_CALL>>>[\s\S]*?<<<END_TOOL_CALL>>>/g;
const TRAILING_TOOL_CALL_PATTERN = /<<<TOOL_CALL>>>[\s\S]*$/;

function cleanProtocolMarkers(text: string): string {
  let cleaned = text.replace(TOOL_CALL_PATTERN, '').replace(TRAILING_TOOL_CALL_PATTERN, '');
  // Also strip any remaining standalone markers
  cleaned = cleaned.replace(/<<<(?:TOOL_CALL|END_TOOL_CALL)>>>/g, '');
  return cleaned.trim();
}

export const TextBlockView = ({ block }: TextBlockViewProps) => {
  // Show cursor only when actively streaming (delivery=stream AND streaming=true)
  const showCursor = block.delivery === 'stream' && block.streaming;
  const displayText = cleanProtocolMarkers(block.text);

  if (!displayText && !showCursor) return null;

  return (
    <div className={`markdown-body text-sm leading-6 text-[var(--duties-text)] ${getDeliveryClassName(block.delivery)}`}>
      {displayText && <ReactMarkdown remarkPlugins={[remarkGfm]}>{displayText}</ReactMarkdown>}
      {showCursor && (
        // [2026-06-01] Why: the streaming cursor used a block Unicode glyph.
        // How: draw the cursor as a small CSS rectangle instead. Purpose: live text
        // rendering does not rely on decorative Unicode symbols.
        <span aria-label="流式输出光标" className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-blue-500 align-middle" />
      )}
    </div>
  );
};
