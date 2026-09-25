import { useEffect, useRef } from 'react';

export class SupersededRequest extends Error {}

export function useRequestScope(scope: string) {
  const current = useRef({ scope, epoch: 0, alive: true });
  if (current.current.scope !== scope) current.current = { scope, epoch: current.current.epoch + 1, alive: true };
  const epoch = current.current.epoch;
  useEffect(() => {
    current.current.alive = true;
    return () => { current.current.alive = false; };
  }, []);
  const isCurrent = () => current.current.alive && current.current.epoch === epoch;
  const check = () => { if (!isCurrent()) throw new SupersededRequest(); };
  return { isCurrent, check };
}
