import { useSyncExternalStore } from 'react';
import { reportPluginError } from './status';
import { guardRequest } from './requestGuard';

export type ExtensionKind = 'views' | 'workspaces' | 'settings' | 'slots' | 'commands' | 'transport';
export interface RegistrationOptions { replace?: boolean; priority?: number }
export interface ExtensionLayer<T = unknown> {
  owner: string;
  generation: string;
  kind: ExtensionKind;
  id: string;
  priority: number;
  sequence: number;
  value?: T;
  wrap?: (next: T) => T;
  replace: boolean;
}

const keyOf = (kind: ExtensionKind, id: string) => `${kind}:${id}`;
const order = (a: ExtensionLayer, b: ExtensionLayer) => a.priority - b.priority || a.owner.localeCompare(b.owner) || a.sequence - b.sequence;

export class ExtensionRegistry {
  private layers: ExtensionLayer[] = [];
  private bases = new Map<string, unknown>();
  private providers = new Map<ExtensionKind, () => Record<string, unknown>>();
  private listeners = new Set<() => void>();
  private revision = 0;
  private sequence = 0;
  private failedWrappers = new WeakSet<ExtensionLayer>();
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  snapshot = () => this.revision;
  private changed() { this.revision += 1; this.listeners.forEach((listener) => listener()); }

  builtin<T>(kind: ExtensionKind, id: string, value: T) { this.bases.set(keyOf(kind, id), value); }
  provideBuiltins(kind: ExtensionKind, provider: () => Record<string, unknown>) { this.providers.set(kind, provider); }
  base<T>(kind: ExtensionKind, id: string): T | undefined { return (this.providers.get(kind)?.()[id] ?? this.bases.get(keyOf(kind, id))) as T | undefined; }
  ids(kind: ExtensionKind): string[] {
    return [...new Set([...this.bases.keys()].filter((key) => key.startsWith(`${kind}:`)).map((key) => key.slice(kind.length + 1)).concat(Object.keys(this.providers.get(kind)?.() || {}), this.layers.filter((layer) => layer.kind === kind).map((layer) => layer.id)))];
  }
  chain<T>(kind: ExtensionKind, id: string, base = this.base<T>(kind, id), strict = false): Array<{ value: T; owner: string; generation: string }> {
    const result: Array<{ value: T; owner: string; generation: string }> = [];
    if (base !== undefined) result.push({ value: kind === 'transport' ? guardRequest(base as Parameters<typeof guardRequest>[0], 'builtin', 'builtin') as T : base, owner: 'builtin', generation: 'builtin' });
    for (const layer of this.layers.filter((item) => item.kind === kind && item.id === id).sort(order)) {
      if (this.failedWrappers.has(layer)) continue;
      const previous = result[result.length - 1]?.value;
      if (layer.wrap && previous === undefined) continue;
      try {
        const value = layer.wrap ? layer.wrap(previous) as T : layer.value as T;
        result.push({ value: kind === 'transport' ? guardRequest(value as Parameters<typeof guardRequest>[0], layer.owner, layer.generation) as T : value, owner: layer.owner, generation: layer.generation });
      }
      catch (error) {
        if (strict) throw error;
        this.failedWrappers.add(layer);
        queueMicrotask(() => reportPluginError(layer.owner, layer.generation, error));
      }
    }
    return result;
  }
  resolve<T>(kind: ExtensionKind, id: string, base = this.base<T>(kind, id)): T | undefined { const chain = this.chain(kind, id, base); return chain[chain.length - 1]?.value; }
  hasContribution(kind: ExtensionKind, id: string) { return this.layers.some((layer) => layer.kind === kind && layer.id === id); }
  remove(owner: string, generation?: string) {
    const next = this.layers.filter((layer) => layer.owner !== owner || (generation !== undefined && layer.generation !== generation));
    if (next.length !== this.layers.length) { this.layers = next; this.changed(); }
  }
  transaction(owner: string, generation: string, priority = 100) {
    let state: 'pending' | 'committed' | 'closed' = 'pending';
    let pending: ExtensionLayer[] = [];
    const add = <T>(kind: ExtensionKind, id: string, value: T | undefined, wrap: ((next: T) => T) | undefined, options: RegistrationOptions = {}) => {
      if (state !== 'pending') throw new Error('Registrations must be declared during activate()');
      if (!id.trim() || !Number.isFinite(options.priority ?? priority)) throw new Error('Invalid registration id or priority');
      if (pending.some((layer) => layer.kind === kind && layer.id === id)) throw new Error(`Duplicate registration: ${kind}/${id}`);
      const layer: ExtensionLayer = { owner, generation, kind, id, value, wrap: wrap as ExtensionLayer['wrap'], replace: Boolean(options.replace || wrap), priority: options.priority ?? priority, sequence: this.sequence++ };
      pending.push(layer);
      return () => {
        if (state === 'pending') pending = pending.filter((item) => item !== layer);
        else if (state === 'committed') { this.layers = this.layers.filter((item) => item !== layer); this.changed(); }
      };
    };
    return {
      register: <T>(kind: ExtensionKind, id: string, value: T, options?: RegistrationOptions) => add(kind, id, value, undefined, options),
      wrap: <T>(kind: ExtensionKind, id: string, wrapper: (next: T) => T, options?: RegistrationOptions) => add(kind, id, undefined, wrapper, options),
      commit: () => {
        if (state !== 'pending') throw new Error('Registration transaction is closed');
        const retained = this.layers.filter((layer) => layer.owner !== owner);
        for (const layer of pending) {
          const exists = this.base(layer.kind, layer.id) !== undefined || retained.some((item) => item.kind === layer.kind && item.id === layer.id);
          if (exists && !layer.replace) throw new Error(`Use replace or wrap for ${layer.kind}/${layer.id}`);
          if (!exists && layer.wrap) throw new Error(`Cannot wrap missing registration: ${layer.kind}/${layer.id}`);
        }
        const previous = this.layers;
        this.layers = retained.concat(pending);
        try { for (const layer of pending) this.chain(layer.kind, layer.id, this.base(layer.kind, layer.id), true); }
        catch (error) { this.layers = previous; throw error; }
        state = 'committed'; this.changed();
      },
      dispose: () => { if (state === 'committed') this.remove(owner, generation); state = 'closed'; pending = []; },
    };
  }
}

export const extensions = new ExtensionRegistry();
export const useExtensionRevision = () => useSyncExternalStore(extensions.subscribe, extensions.snapshot);
