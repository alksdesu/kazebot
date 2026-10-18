import * as React from 'react';
import * as api from '../api/supervisorClient';
import { useChatStore } from '../store/chatStore';
import { useSettingsStore } from '../store/settingsStore';
import { useClientPrefsStore } from '../store/clientPrefsStore';
import { useViewStore } from '../store/viewStore';
import { confirmNavigation, useUnsavedChanges } from '../hooks/useUnsavedChanges';
import type { AppViewDefinition, AppViewContext } from '../views/viewRegistry';
import type { SettingsTabDefinition } from '../components/settings/settingsTabs';
import type { AppLayoutProps } from '../components/layout/AppLayout';
import { executeCommand, type Command } from './commands';
import { extensions, type RegistrationOptions } from './registry';
import type { RequestHandler } from './transport';

export interface WorkspaceDefinition { id: string; label: string; icon?: string; order?: number }
export type SlotRenderer = (context: AppViewContext) => React.ReactNode;
export type LayoutRenderer = (props: AppLayoutProps) => React.ReactNode;
export type PluginDisposer = () => void | Promise<void>;
export interface PluginResources {
  signal: AbortSignal;
  own: (dispose: PluginDisposer) => PluginDisposer;
  release: (dispose: () => void) => () => void;
  style: (css: string) => PluginDisposer;
  asset: (path: string) => Promise<Blob>;
  request: (path: string, init?: RequestInit) => Promise<Response>;
}

export function createPluginSdk(
  identity: { id: string; generation: string; config: Record<string, unknown>; metadata: Record<string, unknown> },
  transaction: ReturnType<typeof extensions.transaction>, resources: PluginResources,
) {
  const registrations = <T>(kind: 'views' | 'workspaces' | 'settings' | 'slots' | 'commands') => ({
    register: (id: string, value: T, options?: RegistrationOptions) => transaction.register(kind, id, value, options),
    wrap: (id: string, wrapper: (next: T) => T, options?: RegistrationOptions) => transaction.wrap(kind, id, wrapper, options),
  });
  const store = <T extends { getState: () => any; subscribe: (...args: any[]) => () => void }>(source: T) => {
    const subscribe = (listener: Parameters<T['subscribe']>[0]) => resources.release(source.subscribe(listener));
    return {
    getState: source.getState,
    subscribe,
    use: <V>(selector: (state: ReturnType<T['getState']>) => V) => {
      const state = React.useSyncExternalStore(subscribe, source.getState);
      return selector(state);
    },
    };
  };
  return Object.freeze({
    version: 1 as const, React, ...identity, signal: resources.signal,
    views: registrations<AppViewDefinition>('views'),
    workspaces: registrations<WorkspaceDefinition>('workspaces'),
    settings: registrations<SettingsTabDefinition>('settings'),
    slots: registrations<SlotRenderer | LayoutRenderer>('slots'),
    commands: { ...registrations<Command>('commands'), execute: executeCommand },
    requests: { wrap: (wrapper: (next: RequestHandler) => RequestHandler, options?: RegistrationOptions) => transaction.wrap('transport', 'request', wrapper, options) },
    state: { chat: store(useChatStore), settings: store(useSettingsStore), preferences: store(useClientPrefsStore), view: store(useViewStore) },
    forms: { useUnsavedChanges, confirmNavigation },
    api, http: { request: resources.request }, assets: {
      fetch: resources.asset,
      url: async (path: string) => { const url = URL.createObjectURL(await resources.asset(path)); resources.release(() => URL.revokeObjectURL(url)); return url; },
    },
    styles: { add: resources.style },
    lifecycle: { onDispose: resources.own },
    events: {
      listen: (target: EventTarget, type: string, listener: EventListener, options?: AddEventListenerOptions) => {
        target.addEventListener(type, listener, options);
        return resources.release(() => target.removeEventListener(type, listener, options));
      },
    },
    timers: {
      interval: (callback: () => void, delay: number) => { const timer = window.setInterval(callback, delay); return resources.release(() => window.clearInterval(timer)); },
      timeout: (callback: () => void, delay: number) => { const timer = window.setTimeout(callback, delay); return resources.release(() => window.clearTimeout(timer)); },
    },
  });
}

export type FrontendPluginSDK = ReturnType<typeof createPluginSdk>;
export interface FrontendPluginModule { activate: (sdk: FrontendPluginSDK) => void | PluginDisposer | Promise<void | PluginDisposer> }
