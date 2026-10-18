import { useSyncExternalStore } from 'react';

export interface BrowserPluginStatus {
  id: string;
  desiredHash: string;
  activeHash?: string;
  generation?: string;
  status: 'loading' | 'loaded' | 'error' | 'disabled';
  errors: string[];
}
export interface BrowserPluginState { settled: boolean; generation: string; plugins: BrowserPluginStatus[]; error: string }
let state: BrowserPluginState = { settled: true, generation: '', plugins: [], error: '' };
const listeners = new Set<() => void>();
export const browserPluginState = () => state;
export const subscribeBrowserPlugins = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export const useBrowserPlugins = () => useSyncExternalStore(subscribeBrowserPlugins, browserPluginState);
export function updateBrowserPlugins(patch: Partial<BrowserPluginState>) { state = { ...state, ...patch }; listeners.forEach((listener) => listener()); }
export function updateBrowserPlugin(id: string, patch: Partial<BrowserPluginStatus>) {
  const previous = state.plugins.find((plugin) => plugin.id === id);
  const next = { id, desiredHash: '', status: 'loading' as const, errors: [], ...previous, ...patch };
  updateBrowserPlugins({ plugins: [...state.plugins.filter((plugin) => plugin.id !== id), next] });
}
export function reportPluginError(owner: string, generation: string, error: unknown) {
  if (owner === 'builtin') return;
  const previous = state.plugins.find((plugin) => plugin.id === owner);
  if (!previous || (previous.generation && previous.generation !== generation)) return;
  updateBrowserPlugin(owner, { status: 'error', errors: [...(previous?.errors || []), error instanceof Error ? error.message : String(error)].slice(-20) });
}
