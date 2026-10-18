import { useLayoutEffect } from 'react';
import { MOUNT } from '../api/supervisorClient';
import { fetchWithDeadline } from '../api/requestDeadline';
import { useSettingsStore } from '../store/settingsStore';
import { extensions } from './registry';
import { createPluginSdk, type FrontendPluginModule, type PluginDisposer } from './sdk';
import { browserPluginState, updateBrowserPlugin, updateBrowserPlugins } from './status';

export interface PluginAsset { path: string; hash: string }
export interface FrontendPlugin {
  id: string; desired_hash: string; package_hash: string; entry: PluginAsset;
  styles: PluginAsset[]; assets?: PluginAsset[]; config: Record<string, unknown>; metadata: Record<string, unknown>;
}
export interface FrontendCatalog {
  generation: string;
  plugins: FrontendPlugin[];
  errors?: Array<{ id: string; error: string; enabled: boolean }>;
}
interface ActivePlugin { hash: string; generation: string; stop: () => void; dispose: () => Promise<void> }
type ModuleLoader = (source: string) => Promise<FrontendPluginModule>;
const message = (error: unknown) => error instanceof Error ? error.message : String(error);
const publishedStyles = new Set<HTMLStyleElement>();
function publishStyle(element: HTMLStyleElement) {
  publishedStyles.add(element);
  const ordered = [...publishedStyles].sort((a, b) => Number(a.dataset.pluginPriority) - Number(b.dataset.pluginPriority) || a.dataset.pluginOwner!.localeCompare(b.dataset.pluginOwner!));
  ordered.forEach((style) => document.head.append(style));
}
function bounded<T>(operation: Promise<T>, signal: AbortSignal | undefined, timeout: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new Error('Plugin lifecycle cancelled'));
    const timer = window.setTimeout(() => reject(new Error('Plugin lifecycle timed out')), timeout);
    if (signal?.aborted) abort();
    signal?.addEventListener('abort', abort, { once: true });
    operation.then(resolve, reject).finally(() => { window.clearTimeout(timer); signal?.removeEventListener('abort', abort); });
  });
}

async function importModule(source: string): Promise<FrontendPluginModule> {
  const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
  try { return await import(/* @vite-ignore */ url); }
  finally { URL.revokeObjectURL(url); }
}
async function verifyAsset(bytes: ArrayBuffer, expected: string) {
  if (!/^[a-f0-9]{64}$/i.test(expected)) throw new Error('Plugin asset has no SHA-256 fingerprint');
  if (!crypto.subtle) throw new Error('Plugin loading requires HTTPS or localhost for integrity verification');
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const hash = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
  if (hash !== expected.toLowerCase()) throw new Error('Plugin asset fingerprint mismatch');
}

export class FrontendPluginRuntime {
  private epoch = 0;
  private token = '';
  private controller = new AbortController();
  private active = new Map<string, ActivePlugin>();
  private inFlight: Promise<void> | null = null;
  private sequence = 0;
  private catalogErrors = new Set<string>();
  constructor(private loader: ModuleLoader = importModule) {}

  start(token: string) {
    this.stop(); this.token = token;
    this.controller = new AbortController();
    updateBrowserPlugins({ settled: false, generation: '', plugins: [], error: '' });
    void this.refresh();
  }
  stop() {
    this.epoch += 1; this.token = ''; this.controller.abort(); this.inFlight = null;
    for (const entry of this.active.values()) { entry.stop(); void entry.dispose(); }
    this.active.clear();
    this.catalogErrors.clear();
    updateBrowserPlugins({ settled: true, plugins: [], generation: '', error: '' });
  }
  refresh(): Promise<void> {
    if (!this.token) return Promise.resolve();
    if (this.inFlight) return this.inFlight;
    const epoch = this.epoch;
    const task = this.synchronize(epoch).finally(() => { if (epoch === this.epoch) this.inFlight = null; });
    this.inFlight = task;
    return task;
  }
  private async request(path: string, signal: AbortSignal) {
    const response = await fetchWithDeadline(`${MOUNT}/v1/admin/plugins${path}`, {
      headers: { Authorization: `Bearer ${this.token}` }, signal,
    }, undefined, true);
    if (!response.ok) throw new Error(`Plugin request failed (${response.status})`);
    return response;
  }
  private async synchronize(epoch: number) {
    try {
      const catalog = await (await this.request('/frontend', this.controller.signal)).json() as FrontendCatalog;
      if (epoch !== this.epoch) return;
      if (!Array.isArray(catalog.plugins)) throw new Error('Invalid frontend plugin catalog');
      const failures = (catalog.errors || []).filter((entry) => entry.enabled);
      const ids = new Set([...catalog.plugins.map((plugin) => plugin.id), ...failures.map((entry) => entry.id)]);
      for (const failure of failures) {
        this.catalogErrors.add(failure.id);
        const active = this.active.get(failure.id);
        updateBrowserPlugin(failure.id, { status: 'error', activeHash: active?.hash, generation: active?.generation, errors: [failure.error] });
      }
      for (const entry of browserPluginState().plugins) {
        if (!ids.has(entry.id) && !this.active.has(entry.id)) {
          this.catalogErrors.delete(entry.id);
          updateBrowserPlugin(entry.id, { status: 'disabled', activeHash: undefined });
        }
      }
      for (const [id, entry] of this.active) {
        if (ids.has(id)) continue;
        entry.stop(); this.active.delete(id); this.catalogErrors.delete(id); await entry.dispose();
        if (epoch !== this.epoch) return;
        updateBrowserPlugin(id, { status: 'disabled', activeHash: undefined });
      }
      for (const plugin of catalog.plugins) {
        if (epoch !== this.epoch) return;
        if (this.active.get(plugin.id)?.hash === plugin.desired_hash) {
          if (this.catalogErrors.delete(plugin.id)) updateBrowserPlugin(plugin.id, { status: 'loaded', desiredHash: plugin.desired_hash, errors: [] });
          continue;
        }
        this.catalogErrors.delete(plugin.id);
        await this.activate(plugin, epoch);
      }
      if (epoch === this.epoch) updateBrowserPlugins({ settled: true, generation: catalog.generation, error: '' });
    } catch (error) {
      if (epoch === this.epoch) updateBrowserPlugins({ settled: true, error: message(error) });
    }
  }
  private async activate(plugin: FrontendPlugin, epoch: number) {
    const generation = `${epoch}:${++this.sequence}:${plugin.desired_hash}`;
    const transaction = extensions.transaction(plugin.id, generation, typeof plugin.metadata.priority === 'number' ? plugin.metadata.priority : 100);
    const controller = new AbortController();
    const parentSignal = this.controller.signal;
    const abort = () => controller.abort();
    parentSignal.addEventListener('abort', abort, { once: true });
    const cleanups: PluginDisposer[] = [];
    const releases = new Set<() => void>();
    const styles: HTMLStyleElement[] = [];
    let committed = false;
    let disposed = false;
    const valid = () => epoch === this.epoch && !controller.signal.aborted;
    const own = (cleanup: PluginDisposer) => {
      if (disposed || !valid()) { void Promise.resolve().then(cleanup).catch(() => {}); throw new Error('Plugin generation has expired'); }
      let done = false;
      const once = () => { if (!done) { done = true; return cleanup(); } };
      cleanups.push(once); return once;
    };
    const release = (cleanup: () => void) => {
      if (disposed || !valid()) { cleanup(); throw new Error('Plugin generation has expired'); }
      const once = () => { if (releases.delete(once)) cleanup(); };
      releases.add(once); return once;
    };
    const releaseAll = () => {
      for (const cleanup of [...releases].reverse()) {
        try { cleanup(); } catch (error) {
          if (epoch === this.epoch) updateBrowserPlugin(plugin.id, { status: 'error', errors: [`Resource cleanup: ${message(error)}`] });
        }
      }
    };
    controller.signal.addEventListener('abort', releaseAll, { once: true });
    const stop = () => { controller.abort(); transaction.dispose(); styles.forEach((style) => style.remove()); };
    const dispose = async () => {
      if (disposed) return;
      disposed = true; parentSignal.removeEventListener('abort', abort);
      for (const cleanup of cleanups.reverse()) {
        try { await bounded(Promise.resolve().then(cleanup), undefined, 5000); }
        catch (error) {
          if (epoch === this.epoch) {
            const previous = browserPluginState().plugins.find((item) => item.id === plugin.id);
            updateBrowserPlugin(plugin.id, { status: 'error', errors: [...(previous?.errors || []), `dispose ${generation}: ${message(error)}`].slice(-20) });
          }
        }
      }
    };
    const asset = async (path: string, hash?: string) => {
      if (!valid()) throw new Error('Plugin generation has expired');
      if (path.split('/').some((part) => !part || part === '.' || part === '..') || path.includes('\\')) throw new Error('Invalid plugin asset path');
      const response = await this.request(`/${encodeURIComponent(plugin.id)}/assets/${encodeURIComponent(plugin.package_hash)}/${path.split('/').map(encodeURIComponent).join('/')}`, controller.signal);
      const bytes = await response.arrayBuffer();
      const fingerprint = hash || [plugin.entry, ...plugin.styles, ...(plugin.assets || [])].find((item) => item.path === path)?.hash;
      if (fingerprint) await verifyAsset(bytes, fingerprint);
      if (!valid()) throw new Error('Plugin generation has expired');
      return new Blob([bytes], { type: response.headers.get('Content-Type') || 'application/octet-stream' });
    };
    const style = (css: string) => {
      if (!valid()) throw new Error('Plugin generation has expired');
      const relativeUrl = [...css.matchAll(/url\(([^)]*)\)/gi)].some((match) => !/^(?:data:|blob:|https?:|\/\/)/i.test(match[1].trim().replace(/^['"]|['"]$/g, '')));
      if (/@import\b/i.test(css) || relativeUrl) throw new Error('Styles must use inline, absolute, or sdk.assets.url() resources; relative URLs and @import are unsupported');
      const element = document.createElement('style');
      element.dataset.pluginOwner = plugin.id; element.dataset.pluginGeneration = generation;
      element.dataset.pluginPriority = String(typeof plugin.metadata.priority === 'number' ? plugin.metadata.priority : 100); element.textContent = css;
      styles.push(element); if (committed) publishStyle(element);
      return release(() => { publishedStyles.delete(element); element.remove(); const at = styles.indexOf(element); if (at >= 0) styles.splice(at, 1); });
    };
    const request = (path: string, init: RequestInit = {}) => {
      if (!valid()) throw new Error('Plugin generation has expired');
      if (!path.startsWith('/') || path.startsWith('//') || path.split(/[/?#]/).some((part) => decodeURIComponent(part) === '..')) throw new Error('SDK request path must be relative to this instance API');
      const headers = new Headers(init.headers);
      headers.set('Authorization', `Bearer ${this.token}`);
      const signal = init.signal ? AbortSignal.any([controller.signal, init.signal]) : controller.signal;
      return fetchWithDeadline(`${MOUNT}/v1${path}`, { ...init, headers, signal });
    };
    updateBrowserPlugin(plugin.id, { desiredHash: plugin.desired_hash, status: 'loading', errors: [] });
    try {
      const source = await (await asset(plugin.entry.path, plugin.entry.hash)).text();
      for (const item of plugin.styles || []) style(await (await asset(item.path, item.hash)).text());
      const module = await bounded(this.loader(source), controller.signal, 30000);
      if (!valid()) throw new Error('Plugin generation has expired');
      if (typeof module.activate !== 'function') throw new Error('Frontend entry must export activate(sdk)');
      const activation = Promise.resolve(module.activate(createPluginSdk({ id: plugin.id, generation, config: plugin.config, metadata: plugin.metadata }, transaction, { signal: controller.signal, own, release, style, asset, request }))).then((cleanup) => {
        if (typeof cleanup === 'function') own(cleanup);
      });
      await bounded(activation, controller.signal, 30000);
      if (!valid()) throw new Error('Plugin generation has expired');
      const previous = this.active.get(plugin.id);
      transaction.commit(); committed = true; styles.forEach(publishStyle);
      this.active.set(plugin.id, { hash: plugin.desired_hash, generation, stop, dispose });
      updateBrowserPlugin(plugin.id, { status: 'loaded', activeHash: plugin.desired_hash, generation, errors: [] });
      if (previous) { previous.stop(); await previous.dispose(); }
    } catch (error) {
      stop(); await dispose();
      if (epoch === this.epoch) updateBrowserPlugin(plugin.id, { status: 'error', errors: [message(error)], activeHash: this.active.get(plugin.id)?.hash });
    }
  }
}

export const frontendPlugins = new FrontendPluginRuntime();
export function useFrontendPlugins() {
  const token = useSettingsStore((state) => state.adminToken);
  const authenticated = useSettingsStore((state) => state.isAuthenticated);
  useLayoutEffect(() => {
    if (!authenticated || !token) return;
    frontendPlugins.start(token);
    const timer = window.setInterval(() => void frontendPlugins.refresh(), 5000);
    return () => { window.clearInterval(timer); frontendPlugins.stop(); };
  }, [authenticated, token]);
}
export const refreshFrontendPlugins = () => frontendPlugins.refresh();
export { browserPluginState };
