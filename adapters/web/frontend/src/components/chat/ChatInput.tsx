// [2026-05-16] Rewritten: added attachment upload flow with file preview and delete,
// max-w-3xl centered, tighter textarea (min-h-16), Ctrl+Enter to send.
import { type FormEvent, type KeyboardEvent, useRef, useState } from 'react';

import type { Attachment } from '../../types';
import { Button, Icon } from '../common';

interface ChatInputProps {
  disabled?: boolean;
  onSend: (text: string, attachments?: Attachment[]) => Promise<void> | void;
}

const formatSize = (bytes: number): string => {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
};

export const ChatInput = ({ disabled = false, onSend }: ChatInputProps) => {
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const canSend = !disabled && (draft.trim().length > 0 || attachments.length > 0);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSend) return;

    const text = draft.trim();
    const currentAttachments = [...attachments];
    setDraft('');
    setAttachments([]);
    await onSend(text, currentAttachments.length > 0 ? currentAttachments : undefined);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Ctrl+Enter or Cmd+Enter to send
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  const handleFileSelect = () => {
    const files = fileInputRef.current?.files;
    if (!files) return;
    const newAttachments: Attachment[] = Array.from(files).map((file) => ({
      name: file.name,
      size: file.size,
      url: URL.createObjectURL(file),
      file,
    }));
    setAttachments((prev) => [...prev, ...newAttachments]);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const removeAttachment = (index: number) => {
    setAttachments((prev) => {
      const removed = prev[index];
      if (removed?.url) URL.revokeObjectURL(removed.url);
      return prev.filter((_, i) => i !== index);
    });
  };

  return (
    <div className="mx-auto max-w-3xl px-2 py-2 sm:px-3 sm:py-3">
      {/* Attachment preview list */}
      {attachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {attachments.map((att, i) => (
            <div
              key={i}
              className="flex items-center gap-1.5 border border-[var(--duties-border)] px-2 py-1 text-xs text-[var(--duties-secondary)]"
            >
              <span className="inline-flex max-w-32 items-center gap-1 truncate">
                {/* [2026-06-01] Why: attachment previews used a paperclip emoji.
                    How: render attach_file through Material Symbols. Purpose: upload
                    previews match the shared icon system used elsewhere. */}
                <Icon name="attach_file" size={14} />
                <span className="truncate">{att.name}</span>
              </span>
              <span className="text-[var(--duties-tertiary)]">({formatSize(att.size)})</span>
              <button
                className="ml-0.5 text-[var(--duties-tertiary)] transition-colors hover:text-[var(--duties-text)]"
                onClick={() => removeAttachment(i)}
                type="button"
              >
                {/* [2026-06-01] Why: attachment removal used a multiplication glyph.
                    How: render close through the shared Icon component. Purpose: small
                    destructive controls no longer rely on literal Unicode symbols. */}
                <Icon name="close" size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
      <form className="grid grid-cols-[auto_1fr_auto] gap-1.5 sm:gap-2" onSubmit={handleSubmit}>
        {/* Attach files button.
            [2026-06-01] Why: the file picker button used a paperclip emoji.
            How: render attach_file with the shared Icon primitive. Purpose: composer
            controls use Material Symbols instead of platform emoji. */}
        <button
          className="self-end border border-[var(--duties-border)] px-2.5 py-2 text-sm text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)] hover:text-[var(--duties-text)]"
          onClick={() => fileInputRef.current?.click()}
          title="添加文件"
          type="button"
        >
          <Icon name="attach_file" size={18} />
        </button>
        <textarea
          className="min-h-12 resize-none border border-[var(--duties-border)] bg-transparent p-2 font-sans text-sm text-[var(--duties-text)] outline-none transition-colors placeholder:text-[var(--duties-tertiary)] focus:border-[var(--duties-text)] sm:min-h-16 sm:p-2.5"
          disabled={disabled}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入消息…"
          value={draft}
        />
        <Button className="self-end min-w-20" disabled={!canSend} type="submit" variant="primary">
          发送
        </Button>
        <input
          accept="*/*"
          className="hidden"
          multiple
          onChange={handleFileSelect}
          ref={fileInputRef}
          type="file"
        />
      </form>
    </div>
  );
};
