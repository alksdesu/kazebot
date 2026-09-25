// Shared modal shell. Every dialog in the app duplicated the same backdrop,
// container, and close-button markup before this.
import { type ReactNode, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useModalFocus } from '../../hooks/useModalFocus';

import { Icon } from './Icon';

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  ariaLabel?: string;
  maxWidth?: string;
  children: ReactNode;
}

export const Modal = ({
  open,
  onClose,
  title,
  subtitle,
  ariaLabel,
  maxWidth = 'max-w-lg',
  children,
}: ModalProps) => {
  const panel = useRef<HTMLDivElement>(null);
  useModalFocus(open, panel, onClose);

  if (!open) return null;

  return createPortal(
    <div className="app-modal-overlay fixed inset-0 z-50 flex items-center justify-center p-4" onMouseDown={onClose}>
      <div
        ref={panel}
        tabIndex={-1}
        aria-label={ariaLabel || title}
        aria-modal="true"
        className={`app-dialog flex max-h-[90dvh] w-full ${maxWidth} flex-col border border-[var(--duties-border)] bg-[var(--duties-surface)] shadow-xl`}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="flex items-center justify-between gap-3 border-b border-[var(--duties-border)] px-4 py-3">
          <div>
            {subtitle && (
              <p className="mb-1 text-xs text-[var(--duties-tertiary)]">{subtitle}</p>
            )}
            <h2 className="text-base font-semibold">{title}</h2>
          </div>
          <button
            aria-label={`关闭${title}`}
            className="app-icon-button shrink-0 text-[var(--duties-secondary)] hover:bg-[var(--duties-muted)]"
            onClick={onClose}
            type="button"
          >
            <Icon name="close" size={18} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {children}
        </div>
      </div>
    </div>, document.body,
  );
};
