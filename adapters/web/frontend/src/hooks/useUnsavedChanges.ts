import { useCallback, useEffect, useRef } from 'react';

const DEFAULT_MESSAGE = '有尚未保存的修改。离开会丢失这些修改，确定继续吗？';
const drafts = new Map<symbol, string>();

function beforeUnload(event: BeforeUnloadEvent) {
  if (!drafts.size) return;
  event.preventDefault();
  event.returnValue = '';
}

export function confirmNavigation(): boolean {
  return !drafts.size || window.confirm(drafts.values().next().value || DEFAULT_MESSAGE);
}

export function useUnsavedChanges(dirty: boolean, message = DEFAULT_MESSAGE): () => boolean {
  const identity = useRef(Symbol('draft'));
  useEffect(() => {
    const key = identity.current;
    if (dirty) {
      drafts.set(key, message);
      window.addEventListener('beforeunload', beforeUnload);
    }
    return () => {
      drafts.delete(key);
      if (!drafts.size) window.removeEventListener('beforeunload', beforeUnload);
    };
  }, [dirty, message]);
  return useCallback(() => !dirty || window.confirm(message), [dirty, message]);
}
