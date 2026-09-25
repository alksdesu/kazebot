import { type PropsWithChildren, type ReactNode, useEffect, useLayoutEffect, useRef, useState } from 'react';

import { useModalFocus } from '../../hooks/useModalFocus';
import { RIGHT_PANEL_DEFAULT_WIDTH, RIGHT_PANEL_MAX_WIDTH, RIGHT_PANEL_MIN_WIDTH, clampRightPanelWidth, useClientPrefsStore } from '../../store/clientPrefsStore';
import { useSettingsStore } from '../../store/settingsStore';
import { useViewStore } from '../../store/viewStore';
import { Icon } from '../common';

const RESIZE_STEP = 16;

function cssPixels(name: string, fallback: number) {
  const style = getComputedStyle(document.documentElement);
  const value = style.getPropertyValue(name).trim();
  const number = Number.parseFloat(value);
  if (!Number.isFinite(number)) return fallback;
  return value.endsWith('rem') ? number * (Number.parseFloat(style.fontSize) || 16) : number;
}

function layoutMetrics() {
  const width = window.innerWidth || document.documentElement.clientWidth;
  return {
    width,
    sidebar: width >= cssPixels('--duties-sidebar-breakpoint', 768),
    rail: width >= cssPixels('--duties-rail-breakpoint', 1280),
    maxWidth: Math.min(RIGHT_PANEL_MAX_WIDTH, Math.max(RIGHT_PANEL_MIN_WIDTH,
      width - cssPixels('--duties-sidebar-w', 240) - cssPixels('--duties-main-min-width', 480) - 4)),
  };
}

interface AppLayoutProps extends PropsWithChildren {
  sidebar: ReactNode;
  header: ReactNode;
  composer?: ReactNode;
  logPanel?: ReactNode;
  rightPanel?: ReactNode;
  navigationKey?: string;
}

export const AppLayout = ({ sidebar, header, composer, logPanel, rightPanel, navigationKey = '', children }: AppLayoutProps) => {
  const [metrics, setMetrics] = useState(layoutMetrics);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { rightPanelOpen, setRightPanelOpen } = useSettingsStore();
  const { rightPanelWidth, setRightPanelWidth } = useClientPrefsStore();
  const [dragWidth, setDragWidth] = useState<number | null>(null);
  const hasRightPanel = Boolean(logPanel || rightPanel);
  const rightVisible = hasRightPanel && rightPanelOpen;
  const rightOverlay = rightVisible && !metrics.rail;
  const leftVisible = metrics.sidebar || (sidebarOpen && !rightOverlay);
  const leftOverlay = leftVisible && !metrics.sidebar;
  const panelWidth = Math.min(clampRightPanelWidth(dragWidth ?? rightPanelWidth), metrics.maxWidth);
  const sidebarRef = useRef<HTMLElement>(null);
  const rightRef = useRef<HTMLElement>(null);
  const stopResize = useRef<(() => void) | null>(null);
  const touchStart = useRef<{ x: number; y: number } | null>(null);
  const viewMode = useViewStore(state => state.viewMode);
  const settingsTab = useViewStore(state => state.activeSettingsTab);
  const desktopOpen = useRef(rightPanelOpen);
  const previousRail = useRef(metrics.rail);
  const closeLeft = () => setSidebarOpen(false);
  const closeRight = () => setRightPanelOpen(false);
  useModalFocus(leftOverlay, sidebarRef, closeLeft);
  useModalFocus(rightOverlay, rightRef, closeRight);

  useLayoutEffect(() => {
    if (!metrics.rail) setRightPanelOpen(false);
    const resize = () => setMetrics(layoutMetrics());
    window.addEventListener('resize', resize);
    return () => { window.removeEventListener('resize', resize); stopResize.current?.(); };
  }, [setRightPanelOpen]);
  useLayoutEffect(() => {
    if (previousRail.current === metrics.rail) return;
    stopResize.current?.();
    if (metrics.rail) setRightPanelOpen(desktopOpen.current);
    else { desktopOpen.current = rightPanelOpen; setRightPanelOpen(false); }
    setSidebarOpen(false);
    previousRail.current = metrics.rail;
  }, [metrics.rail, rightPanelOpen, setRightPanelOpen]);
  useEffect(() => {
    setSidebarOpen(false);
    if (!metrics.rail) setRightPanelOpen(false);
  }, [viewMode, settingsTab, navigationKey, metrics.rail, setRightPanelOpen]);
  useEffect(() => { if (!hasRightPanel) stopResize.current?.(); }, [hasRightPanel]);

  const beginResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || !metrics.rail) return;
    event.preventDefault(); stopResize.current?.();
    const startX = event.clientX;
    const startWidth = panelWidth;
    const widthAt = (x: number) => Math.min(metrics.maxWidth, clampRightPanelWidth(startWidth - (x - startX)));
    const controller = new AbortController();
    const cleanup = () => { controller.abort(); stopResize.current = null; setDragWidth(null); };
    stopResize.current = cleanup;
    window.addEventListener('pointermove', move => setDragWidth(widthAt(move.clientX)), { signal: controller.signal });
    window.addEventListener('pointerup', up => { setRightPanelWidth(widthAt(up.clientX)); cleanup(); }, { signal: controller.signal });
    window.addEventListener('pointercancel', cleanup, { signal: controller.signal });
    window.addEventListener('blur', cleanup, { signal: controller.signal });
  };
  const onResizeKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const width = event.key === 'ArrowLeft' ? panelWidth + RESIZE_STEP : event.key === 'ArrowRight' ? panelWidth - RESIZE_STEP : event.key === 'Home' ? RIGHT_PANEL_DEFAULT_WIDTH : event.key === 'End' ? metrics.maxWidth : null;
    if (width === null) return;
    event.preventDefault(); setRightPanelWidth(Math.min(width, metrics.maxWidth));
  };
  const openLeft = () => { setRightPanelOpen(false); setSidebarOpen(true); };
  const toggleRight = () => { setSidebarOpen(false); setRightPanelOpen(!rightVisible); };
  const handleTouchStart = (event: React.TouchEvent<HTMLDivElement>) => {
    const touch = event.touches[0];
    if (touch) touchStart.current = { x: touch.clientX, y: touch.clientY };
  };
  const handleTouchEnd = (event: React.TouchEvent<HTMLDivElement>) => {
    const start = touchStart.current; const touch = event.changedTouches[0]; touchStart.current = null;
    if (!start || !touch || metrics.rail) return;
    const dx = touch.clientX - start.x; const dy = touch.clientY - start.y;
    if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy)) return;
    if (dx > 0) {
      if (rightOverlay) closeRight();
      else if (!metrics.sidebar && start.x <= 48) openLeft();
    } else if (leftOverlay) closeLeft();
    else if (hasRightPanel && start.x >= metrics.width - 48) { setSidebarOpen(false); setRightPanelOpen(true); }
  };

  return <div className="app-shell flex h-[100dvh] min-h-0 overflow-hidden bg-[var(--duties-bg)] text-[var(--duties-text)]" data-testid="app-layout-root"
    onTouchStart={handleTouchStart} onTouchEnd={handleTouchEnd}
    style={{ '--duties-right-w': `${panelWidth}px` } as React.CSSProperties}>
    {(leftOverlay || rightOverlay) && <div data-overlay-backdrop="true" data-testid="panel-backdrop" className="app-modal-overlay fixed inset-0 z-30" onClick={leftOverlay ? closeLeft : closeRight} />}
    <aside ref={sidebarRef} id="app-navigation" aria-label="主导航" aria-modal={leftOverlay || undefined} role={leftOverlay ? 'dialog' : undefined} tabIndex={-1} hidden={!leftVisible}
      className={`app-sidebar shrink-0 border-r border-[var(--duties-border)] bg-[var(--duties-panel)] ${metrics.sidebar ? 'relative' : 'fixed inset-y-0 left-0 z-40'} ${leftVisible ? 'translate-x-0' : '-translate-x-full'}`}>
      {!metrics.sidebar && <div className="flex justify-end border-b border-[var(--duties-border)] px-2"><button type="button" className="app-icon-button" aria-label="关闭导航" onClick={closeLeft}><Icon name="close" /></button></div>}
      <div className="min-h-0 flex-1">{sidebar}</div>
    </aside>
    <main id="app-main" className="flex min-w-0 flex-1 flex-col" tabIndex={-1}>
      <div className="flex shrink-0 items-center border-b border-[var(--duties-border)] bg-[var(--duties-surface)]">
        {!metrics.sidebar && <button type="button" className="app-icon-button ml-1 shrink-0" aria-label="打开导航" aria-controls="app-navigation" aria-expanded={leftOverlay} onClick={openLeft}><Icon name="menu" size={22} /></button>}
        <div className="min-w-0 flex-1">{header}</div>
        {hasRightPanel && <button type="button" className="app-icon-button mr-1 shrink-0" aria-controls="app-right-panel" aria-expanded={rightVisible} onClick={toggleRight} title={rightVisible ? '收起面板' : '展开面板'} aria-label={rightVisible ? '收起面板' : '展开面板'}><Icon name={rightVisible ? 'chevron_right' : 'chevron_left'} size={20} /></button>}
      </div>
      <section className="relative min-h-0 flex-1 overflow-hidden">{children}</section>
      {composer && <div className="shrink-0 border-t border-[var(--duties-border)] bg-[var(--duties-surface)]">{composer}</div>}
    </main>
    {hasRightPanel && rightVisible && metrics.rail && <div role="separator" aria-label="调整右侧面板宽度" aria-orientation="vertical" aria-valuemax={metrics.maxWidth} aria-valuemin={RIGHT_PANEL_MIN_WIDTH} aria-valuenow={panelWidth} tabIndex={0}
      className="app-resize-handle w-1 shrink-0 cursor-col-resize bg-[var(--duties-border)] hover:bg-[var(--duties-secondary)]" onDoubleClick={() => setRightPanelWidth(RIGHT_PANEL_DEFAULT_WIDTH)} onKeyDown={onResizeKey} onPointerDown={beginResize} title="拖动调整宽度，双击复位" />}
    {hasRightPanel && <aside ref={rightRef} id="app-right-panel" aria-label="右侧面板" role={rightOverlay ? 'dialog' : undefined} aria-modal={rightOverlay || undefined} tabIndex={-1} hidden={!rightVisible}
      className={`app-right-panel shrink-0 flex-col overflow-hidden border-l border-[var(--duties-border)] bg-[var(--duties-surface)] ${metrics.rail ? 'relative w-[var(--duties-right-w)]' : 'app-drawer fixed inset-y-0 right-0 z-40'} ${rightVisible ? 'flex translate-x-0' : 'translate-x-full'}`}>
      {!metrics.rail && <div className="flex shrink-0 items-center justify-between border-b border-[var(--duties-border)] px-4 py-1"><span className="text-sm font-semibold">详情与操作</span><button className="app-icon-button" type="button" aria-label="关闭右侧面板" onClick={closeRight}><Icon name="close" /></button></div>}
      {logPanel ? <><div className="flex h-[60%] min-h-0 shrink-0 flex-col overflow-hidden border-b border-[var(--duties-border)]">{rightPanel}</div><div aria-label="事件日志面板" className="flex h-[40%] min-h-0 shrink-0 flex-col overflow-hidden">{logPanel}</div></> : <div className="flex h-full min-h-0 flex-1 flex-col overflow-hidden">{rightPanel}</div>}
    </aside>}
  </div>;
};
