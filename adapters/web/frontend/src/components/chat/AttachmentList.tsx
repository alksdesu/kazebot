// 消息里的附件。工作区里的文件带令牌取回再显示或下载；外部链接只给链接，不自动加载。
import { useState } from 'react';

import { attachmentUrl } from '../../api/supervisorClient';
import { downloadFile, isInlineViewable } from '../../api/blobs';
import { errorMessage } from '../../api/request';
import { AuthorizedImage } from '../common/AuthorizedImage';
import { Icon } from '../common';

export interface AttachmentView {
  name: string;
  size?: number;
  url?: string;
  type?: string;
  path?: string;
  mime_type?: string;
}

const CHIP_CLASS = 'inline-flex items-center gap-1 border border-[var(--duties-border)] px-2 py-0.5 text-xs text-[var(--duties-secondary)] transition-colors hover:border-[var(--duties-text)]';

function isImage(attachment: AttachmentView): boolean {
  return attachment.type === 'image' || Boolean(attachment.mime_type?.startsWith('image/'));
}

// 浏览器本地的预览地址（刚选的文件）可以直接显示，不会把对话内容带出去。
const isLocalUrl = (url: string): boolean => /^(?:blob|data):/i.test(url);
const isWebUrl = (url: string): boolean => /^https?:\/\//i.test(url);

export function formatAttachmentMeta(attachment: AttachmentView): string {
  if (!attachment.size) return attachment.name;
  const kb = attachment.size / 1024;
  const size = kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(kb))} KB`;
  return `${attachment.name} (${size})`;
}

const WorkspaceImage = ({ attachment }: { attachment: AttachmentView & { path: string } }) => (
  <AuthorizedImage
    alt={attachment.name}
    className="max-h-64 border border-[var(--duties-border)]"
    fallback={() => <span className={CHIP_CLASS} role="status"><Icon name="error" size={14} /><span>{attachment.name}（图片读取失败）</span></span>}
    source={attachmentUrl(attachment.path)}
  >
    {(image, state) => (isInlineViewable(state.type)
      ? <a className="block" href={state.url} rel="noopener noreferrer" target="_blank">{image}</a>
      : image)}
  </AuthorizedImage>
);

const WorkspaceFile = ({ attachment }: { attachment: AttachmentView & { path: string } }) => {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const download = async () => {
    if (busy) return;
    setBusy(true); setError('');
    try { await downloadFile(attachmentUrl(attachment.path), attachment.name || attachment.path.split('/').pop() || 'attachment'); }
    catch (failure) { setError(errorMessage(failure, '附件下载失败')); }
    finally { setBusy(false); }
  };
  return (
    <span className="inline-flex flex-col gap-0.5">
      <button aria-busy={busy || undefined} className={CHIP_CLASS} disabled={busy} onClick={() => void download()} type="button">
        <Icon name="attach_file" size={14} />
        <span>{formatAttachmentMeta(attachment)}</span>
      </button>
      {error && <span className="text-xs text-[var(--duties-danger)]" role="alert">{error}</span>}
    </span>
  );
};

const LinkedAttachment = ({ attachment }: { attachment: AttachmentView }) => {
  const url = attachment.url || '';
  if (isImage(attachment) && isLocalUrl(url)) {
    return <img alt={attachment.name} className="max-h-64 border border-[var(--duties-border)]" src={url} />;
  }
  const href = isWebUrl(url) || isLocalUrl(url) ? url : undefined;
  return (
    <a className={`${CHIP_CLASS} ${href ? '' : 'pointer-events-none opacity-70'}`} href={href} rel="noopener noreferrer" target="_blank">
      <Icon name={isImage(attachment) ? 'open_in_new' : 'attach_file'} size={14} />
      <span>{formatAttachmentMeta(attachment)}</span>
    </a>
  );
};

export const AttachmentList = ({ attachments }: { attachments: readonly AttachmentView[] }) => {
  if (!attachments.length) return null;
  return (
    <footer className="mt-2 flex flex-wrap gap-2">
      {attachments.map((attachment, index) => {
        const key = `${attachment.name}-${index}`;
        if (attachment.path) {
          const located = { ...attachment, path: attachment.path };
          return isImage(attachment) ? <WorkspaceImage attachment={located} key={key} /> : <WorkspaceFile attachment={located} key={key} />;
        }
        return <LinkedAttachment attachment={attachment} key={key} />;
      })}
    </footer>
  );
};
