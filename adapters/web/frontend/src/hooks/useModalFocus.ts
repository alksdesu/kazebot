import { type RefObject, useLayoutEffect, useRef } from 'react';

const layers: symbol[] = [];
const locks = new Map<Element, { count: number; inert: boolean; hidden: string | null }>();
let scrollLocks = 0;
let previousOverflow = '';
const FOCUSABLE = 'button, a[href], input, select, textarea, summary, [contenteditable="true"], [tabindex]';

function controls(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(element => {
    if (element.matches(':disabled, input[type="hidden"]') || (element.hasAttribute('tabindex') && element.tabIndex < 0) || element.closest('[hidden], [inert]')) return false;
    const style = getComputedStyle(element);
    if (style.visibility === 'hidden' || style.visibility === 'collapse') return false;
    let ancestor: HTMLElement | null = element;
    while (ancestor) {
      if (getComputedStyle(ancestor).display === 'none' || getComputedStyle(ancestor).contentVisibility === 'hidden') return false;
      if (ancestor.tagName === 'DETAILS' && !ancestor.hasAttribute('open')) {
        const summary = [...ancestor.children].find(child => child.tagName === 'SUMMARY');
        if (!summary?.contains(element)) return false;
      }
      if (ancestor === container) break;
      ancestor = ancestor.parentElement;
    }
    return typeof element.checkVisibility !== 'function' || element.checkVisibility({ checkVisibilityCSS: true });
  });
}

export function useModalFocus(open: boolean, container: RefObject<HTMLElement>, onClose: () => void) {
  const close = useRef(onClose);
  close.current = onClose;

  useLayoutEffect(() => {
    const panel = container.current;
    if (!open || !panel) return;
    const previous = document.activeElement as HTMLElement | null;
    const identity = Symbol('dialog');
    layers.push(identity);
    const background: Element[] = [];
    let branch: Element = panel;
    while (branch.parentElement) {
      for (const element of branch.parentElement.children) {
        if (element === branch || element.hasAttribute('data-overlay-backdrop') || element.tagName === 'SCRIPT' || element.tagName === 'STYLE') continue;
        const existing = locks.get(element);
        locks.set(element, existing ? { ...existing, count: existing.count + 1 } : { count: 1, inert: element.hasAttribute('inert'), hidden: element.getAttribute('aria-hidden') });
        background.push(element);
        element.setAttribute('inert', '');
        element.setAttribute('aria-hidden', 'true');
      }
      if (branch.parentElement === document.body) break;
      branch = branch.parentElement;
    }
    if (!scrollLocks++) previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const isTop = () => layers[layers.length - 1] === identity;
    const focusFirst = () => {
      const items = controls(panel);
      (items.find(item => item.matches('[data-autofocus], [autofocus]')) || items[0] || panel).focus();
    };
    if (!panel.contains(document.activeElement)) focusFirst();
    const keydown = (event: KeyboardEvent) => {
      if (!isTop()) return;
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); close.current();
      } else if (event.key === 'Tab') {
        const items = controls(panel);
        const first = items[0] || panel;
        const last = items[items.length - 1] || panel;
        if (!panel.contains(document.activeElement) || (event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last) || !items.length) {
          event.preventDefault(); (event.shiftKey ? last : first).focus();
        }
      }
    };
    const focusin = () => { if (isTop() && !panel.contains(document.activeElement)) focusFirst(); };
    document.addEventListener('keydown', keydown, true);
    document.addEventListener('focusin', focusin);
    return () => {
      layers.splice(layers.indexOf(identity), 1);
      document.removeEventListener('keydown', keydown, true);
      document.removeEventListener('focusin', focusin);
      for (const element of background) {
        const saved = locks.get(element);
        if (!saved) continue;
        if (--saved.count) continue;
        locks.delete(element);
        if (!saved.inert) element.removeAttribute('inert');
        if (saved.hidden === null) element.removeAttribute('aria-hidden');
        else element.setAttribute('aria-hidden', saved.hidden);
      }
      if (!--scrollLocks) document.body.style.overflow = previousOverflow;
      // React 会在提交时恢复仍留在 DOM 中的抽屉焦点，回焦要等本次提交结束。
      queueMicrotask(() => {
        if (previous?.isConnected && !previous.closest('[hidden], [inert]')) previous.focus();
        else if (!layers.length) document.querySelector<HTMLElement>('#app-main:not([inert])')?.focus();
      });
    };
  }, [open, container]);
}
