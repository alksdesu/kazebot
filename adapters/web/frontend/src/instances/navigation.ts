import { useSyncExternalStore } from 'react';
import { instanceConsoleHref, isInstancePath, MOUNT } from './paths';

const RECENT_INSTANCE_KEY = 'clonoth_recent_instance';
const RECENT_INSTANCE_EVENT = 'clonoth-recent-instance';

export function isInstanceEntry(): boolean {
  return new URLSearchParams(window.location.search).get('entry') === 'instances';
}

export function instanceEntryHref(): string {
  return `${MOUNT}/web/?entry=instances`;
}

export function getRecentInstance(): string | null {
  try {
    const path = localStorage.getItem(RECENT_INSTANCE_KEY);
    return isInstancePath(path) ? path : null;
  } catch {
    return null;
  }
}

export function rememberInstance(path: string): void {
  if (!isInstancePath(path)) return;
  try {
    localStorage.setItem(RECENT_INSTANCE_KEY, path);
    window.dispatchEvent(new Event(RECENT_INSTANCE_EVENT));
  }
  catch { return; }
}

function subscribeRecentInstance(notify: () => void): () => void {
  const storage = (event: StorageEvent) => { if (!event.key || event.key === RECENT_INSTANCE_KEY) notify(); };
  window.addEventListener('storage', storage);
  window.addEventListener('focus', notify);
  window.addEventListener('pageshow', notify);
  window.addEventListener(RECENT_INSTANCE_EVENT, notify);
  return () => {
    window.removeEventListener('storage', storage);
    window.removeEventListener('focus', notify);
    window.removeEventListener('pageshow', notify);
    window.removeEventListener(RECENT_INSTANCE_EVENT, notify);
  };
}

export function useRecentInstance(): string | null {
  return useSyncExternalStore(subscribeRecentInstance, getRecentInstance, () => null);
}

export function clearInstanceToken(): void {
  const query = new URLSearchParams(window.location.search);
  let changed = false;
  for (const key of [...query.keys()]) {
    if (key.toLowerCase() === 'token') { query.delete(key); changed = true; }
  }
  if (!changed) return;
  const search = query.toString();
  window.history.replaceState(window.history.state, '', `${window.location.pathname}${search ? `?${search}` : ''}${window.location.hash}`);
}

export function navigateToInstance(path: string, preserveLocation = true): boolean {
  if (!isInstancePath(path)) return false;
  clearInstanceToken();
  window.location.assign(instanceConsoleHref(path, preserveLocation));
  return true;
}
