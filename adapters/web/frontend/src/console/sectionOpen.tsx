// 折叠分区的展开状态：按浏览器记住，深链指名的那一块无条件展开并滚过去。平台页和全局默认页共用。
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

import { useViewStore } from '../store/viewStore';

type OpenMap = Record<string, boolean>;

function readOpenMap(storeKey: string): OpenMap {
  try {
    const parsed = JSON.parse(localStorage.getItem(storeKey) || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(([, open]) => typeof open === 'boolean'),
    ) as OpenMap;
  } catch {
    // 存坏了就按默认展开状态来，不该把整页拖垮。
    return {};
  }
}

function writeOpenMap(storeKey: string, value: OpenMap): void {
  try {
    localStorage.setItem(storeKey, JSON.stringify(value));
  } catch {
    // 隐私模式下 localStorage 会抛，记不住展开状态不影响配置本身。
  }
}

export interface SectionOpenState {
  isOpen: (id: string) => boolean;
  toggle: (id: string) => void;
  /** 深链指名的分区 id，没有是空串。 */
  requested: string;
}

export function useSectionOpenState(storeKey: string): SectionOpenState {
  const requested = useViewStore((state) => state.activeSettingsSection);
  const [openMap, setOpenMap] = useState<OpenMap>(() => readOpenMap(storeKey));

  const toggle = useCallback((id: string) => {
    setOpenMap((current) => {
      const next = { ...current, [id]: !current[id] };
      writeOpenMap(storeKey, next);
      return next;
    });
  }, [storeKey]);

  // 深链指名的那一块无条件展开，记住的收起状态不该挡住它。
  useEffect(() => {
    if (!requested) return;
    setOpenMap((current) => (current[requested] ? current : { ...current, [requested]: true }));
  }, [requested]);

  const isOpen = useCallback((id: string) => Boolean(openMap[id]), [openMap]);
  return { isOpen, requested, toggle };
}

/** 深链进来要滚到这一块。等展开渲染完再滚，否则量到的是收起时的位置。 */
export const SectionAnchor = ({ children, deepLinked, open }: {
  children: ReactNode;
  deepLinked: boolean;
  open: boolean;
}) => {
  const anchor = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!deepLinked || !open) return;
    // 可选调用：jsdom 和一些内嵌浏览器没有这个方法，缺它不该把整页打挂。
    anchor.current?.scrollIntoView?.({ block: 'start' });
  }, [deepLinked, open]);
  return <div ref={anchor}>{children}</div>;
};
