// This setup file is added to make Vitest understand DOM assertions from Testing Library.
// It changes the test runtime by loading jest-dom once and cleaning rendered DOM after each test.
// The cleanup hook prevents one React render from leaking into the next UI test while the Supervisor service remains mocked.
// The purpose is to verify the React skeleton without connecting to a browser or the real Supervisor service.
import { cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';

// Node 22+ 自带实验性 localStorage 全局，未配 --localstorage-file 时它没有 getItem，
// 且会盖掉 jsdom 的实现 —— 任何在模块顶层读 localStorage 的 store 都会在 import 期崩。
function installMemoryStorage(key: 'localStorage' | 'sessionStorage'): void {
  const existing = (globalThis as any)[key];
  if (existing && typeof existing.getItem === 'function') return;
  const store = new Map<string, string>();
  const storage: Storage = {
    get length() { return store.size; },
    clear: () => store.clear(),
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    key: (i: number) => [...store.keys()][i] ?? null,
    removeItem: (k: string) => { store.delete(k); },
    setItem: (k: string, v: string) => { store.set(k, String(v)); },
  };
  Object.defineProperty(globalThis, key, { value: storage, configurable: true, writable: true });
}

installMemoryStorage('localStorage');
installMemoryStorage('sessionStorage');

afterEach(() => {
  cleanup();
});
