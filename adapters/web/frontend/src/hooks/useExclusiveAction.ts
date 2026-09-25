import { useRef, useState } from 'react';

export function useExclusiveAction() {
  const pending = useRef(false);
  const [busy, setBusy] = useState(false);
  const run = async (action: () => Promise<void>) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    try { await action(); }
    finally { pending.current = false; setBusy(false); }
  };
  return { busy, run };
}
