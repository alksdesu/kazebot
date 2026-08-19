// [2026-05-16] Stream preview — thinking chain, text preview, tool logs, timer, retry info.
import { useEffect, useState } from 'react';

import type { StreamPreviewState } from '../../types';
import { Icon } from '../common';

interface StreamPreviewProps {
  preview: StreamPreviewState;
}

export const StreamPreview = ({ preview }: StreamPreviewProps) => {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!preview.thinkingStartTime) { setElapsed(0); return; }
    const iv = setInterval(() => {
      setElapsed(Math.floor((Date.now() - (preview.thinkingStartTime || Date.now())) / 1000));
    }, 1000);
    return () => clearInterval(iv);
  }, [preview.thinkingStartTime]);

  if (!preview.isActive) return null;

  return (
    <div className="mx-auto max-w-3xl px-2 py-2 sm:px-4">
      <div className="border border-[var(--duties-border)] bg-[var(--duties-muted)] p-3">
        {/* Timer */}
        <div className="mb-2 flex items-center gap-2 font-mono text-[0.65rem] text-[var(--duties-tertiary)]">
          <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />
          <span>生成中</span>
          {elapsed > 0 && (
            <span className="inline-flex items-center gap-1">
              {/* [2026-06-01] Why: replace the stopwatch emoji with Material Symbols.
                  How: render timer before the elapsed seconds. Purpose: live preview
                  status uses the same icon font as other operational UI. */}
              <Icon name="timer" size={13} />
              <span>{elapsed}s</span>
            </span>
          )}
        </div>

        {/* Retry info */}
        {preview.retryInfo && (
          <div className="mb-2 border-l-2 border-orange-400 bg-orange-50 px-2 py-1 text-xs text-orange-700">
            {preview.retryInfo}
          </div>
        )}

        {/* Thinking preview */}
        {preview.thinkingPreview && (
          <details className="mb-2" open>
            <summary className="cursor-pointer font-mono text-[0.6rem] uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">
              思考中
            </summary>
            <pre className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap text-[0.7rem] text-[var(--duties-secondary)]">
              {preview.thinkingPreview}
            </pre>
          </details>
        )}

        {/* Text preview */}
        {preview.textPreview && (
          <div className="mb-2 whitespace-pre-wrap text-sm text-[var(--duties-text)]">
            {preview.textPreview}
            {/* [2026-06-01] Why: the live preview cursor used a block Unicode glyph.
                How: draw a small CSS rectangle instead. Purpose: stream previews avoid
                decorative Unicode while keeping the same visible cursor behavior. */}
            <span aria-label="流式输出光标" className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-blue-500 align-middle" />
          </div>
        )}

        {/* Tool execution logs */}
        {preview.progressLines.length > 0 && (
          <div className="mt-2">
            <p className="mb-1 font-mono text-[0.6rem] uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">工具日志</p>
            <div className="max-h-32 overflow-y-auto bg-[var(--duties-bg)] p-2 font-mono text-[0.65rem] text-[var(--duties-secondary)]">
              {preview.progressLines.map((line, i) => (
                <div key={i} className="flex items-center gap-1">
                  <span className="mr-1 text-[var(--duties-tertiary)]">{preview.progressLines.length - 5 + i > 0 ? preview.progressLines.length - 5 + i : i + 1}|</span>
                  {/* [2026-06-01] Why: chatStore no longer stores a wrench emoji in progress text.
                      How: render build in the stream preview view layer. Purpose: store
                      state remains plain text while the UI still shows a tool icon. */}
                  <Icon name="build" size={13} />
                  <span>{line}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
