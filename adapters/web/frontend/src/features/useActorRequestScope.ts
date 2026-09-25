import { useCallback, useRef, useSyncExternalStore } from 'react';
import { useSettingsStore } from '../store/settingsStore';
import { SupersededRequest, useRequestScope } from './asyncState';

export function useActorRequestScope(scope: string) {
  const actor = useRef({ token: useSettingsStore.getState().adminToken, version: 0 });
  const readVersion = useCallback(() => {
    const token = useSettingsStore.getState().adminToken;
    if (actor.current.token !== token) actor.current = { token, version: actor.current.version + 1 };
    return actor.current.version;
  }, []);
  const subscribe = useCallback((notify: () => void) => useSettingsStore.subscribe(() => {
    const previous = actor.current.version;
    if (readVersion() !== previous) notify();
  }), [readVersion]);
  const version = useSyncExternalStore(subscribe, readVersion, readVersion);
  const key = JSON.stringify([scope, version]);
  const identity = useRequestScope(key);
  const isCurrent = () => readVersion() === version && identity.isCurrent();
  const check = () => { if (!isCurrent()) throw new SupersededRequest(); };
  return { key, isCurrent, check };
}
