// [2026-06-01] Right-panel tests document the dashboard plus modal workflow.
// Why: header node/model labels now open session editing in an overlay while the
// chat right rail keeps the system dashboard. How: assert the modal is shown without
// changing viewStore and verify the layout split still renders. Purpose: avoid
// returning to right-panel session overrides.
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AppLayout, Header } from '../components/layout';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';

describe('view-mode session configuration entry points', () => {
  beforeEach(() => {
    localStorage.clear();
    useViewStore.setState({ viewMode: 'chat', activeSettingsTab: 'general' });
    useSettingsStore.setState({
      adminToken: 'test-token',
      isAuthenticated: true,
      isConnected: true,
      entryNodeId: 'ereuna_main',
      availableNodes: [
        { id: 'ereuna_main', type: 'ai', name: 'EreunaMain', model: 'gpt-4.1' },
        { id: 'bootstrap.coder', type: 'ai', name: 'Coder', model: 'claude-sonnet-4-5' },
      ],
      modelConfig: null,
      rightPanelOpen: true,
      activeNodeId: 'ereuna_main',
      activeNodeIsOverride: false,
      defaultNodeId: 'ereuna_main',
      globalModel: 'gpt-4o-mini',
      globalBaseUrl: 'https://api.example.test/v1',
      sessionProviderOverride: null,
    });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      node_id: 'ereuna_main',
      is_override: false,
      default_node_id: 'ereuna_main',
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('opens the node session modal from the header node text', () => {
    render(<Header isGenerating={false} sessionId="session-1" title="Test" />);

    fireEvent.click(screen.getByTitle('切换节点'));

    expect(useViewStore.getState().viewMode).toBe('chat');
    expect(useViewStore.getState().activeSettingsTab).toBe('general');
    expect(screen.getByRole('dialog', { name: '节点配置' })).toBeInTheDocument();
    expect(screen.getByText('会话编辑')).toBeInTheDocument();
  });

  it('opens the model session modal from the header model text', () => {
    render(<Header isGenerating={false} sessionId="session-1" title="Test" />);

    fireEvent.click(screen.getByTitle('模型配置'));

    expect(useViewStore.getState().viewMode).toBe('chat');
    expect(useViewStore.getState().activeSettingsTab).toBe('general');
    expect(screen.getByRole('dialog', { name: '模型配置' })).toBeInTheDocument();
    expect(screen.getByText('会话编辑')).toBeInTheDocument();
  });

  it('renders the right panel split and allows the composer slot to be omitted', () => {
    render(
      <AppLayout
        header={<div>头部</div>}
        logPanel={<section aria-label="事件日志内容">事件</section>}
        rightPanel={<section aria-label="系统仪表盘">系统仪表盘</section>}
        sidebar={<nav>侧边栏</nav>}
      >
        <main>聊天</main>
      </AppLayout>,
    );

    expect(screen.getByLabelText('系统仪表盘')).toBeInTheDocument();
    expect(screen.getByLabelText('事件日志面板')).toBeInTheDocument();
    expect(screen.queryByText('输入区')).not.toBeInTheDocument();
  });

  it('renders a full-height right panel when no log panel is provided', () => {
    render(
      <AppLayout
        header={<div>头部</div>}
        rightPanel={<section aria-label="设置右栏">设置右栏</section>}
        sidebar={<nav>侧边栏</nav>}
      >
        <main>设置</main>
      </AppLayout>,
    );

    // [2026-06-02] Regression coverage for Settings right-panel height. Why: without a
    // log panel, AppLayout should not keep the old 60 percent wrapper. How: assert the
    // content wrapper uses the full-height class and no event-log landmark is rendered.
    // Purpose: settings contextual editors can use the whole right column.
    const rightPanel = screen.getByLabelText('设置右栏');
    expect(screen.queryByLabelText('事件日志面板')).not.toBeInTheDocument();
    expect(rightPanel.parentElement).toHaveClass('h-full');
    expect(rightPanel.parentElement).not.toHaveClass('h-[60%]');
  });

  it('opens and closes mobile side panels with horizontal touch gestures', () => {
    useSettingsStore.getState().setRightPanelOpen(false);
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 400 });

    render(
      <AppLayout
        header={<div>头部</div>}
        rightPanel={<section aria-label="右栏内容">右栏内容</section>}
        sidebar={<nav>侧边栏</nav>}
      >
        <main>内容</main>
      </AppLayout>,
    );

    const root = screen.getByTestId('app-layout-root');

    // [2026-06-02] Regression coverage for touch gestures. Why: mobile users need
    // swipe access to side panels without hitting small header buttons. How: simulate
    // edge-starting horizontal touch events and read the shared right-panel store plus
    // overlay classes. Purpose: gesture behavior remains stable across layout refactors.
    fireEvent.touchStart(root, { touches: [{ clientX: 12, clientY: 120 }] });
    fireEvent.touchEnd(root, { changedTouches: [{ clientX: 90, clientY: 124 }] });
    expect(screen.getByText('侧边栏').closest('aside')).toHaveClass('translate-x-0');

    fireEvent.touchStart(root, { touches: [{ clientX: 180, clientY: 120 }] });
    fireEvent.touchEnd(root, { changedTouches: [{ clientX: 80, clientY: 124 }] });
    expect(screen.getByText('侧边栏').closest('aside')).toHaveClass('-translate-x-full');

    fireEvent.touchStart(root, { touches: [{ clientX: 360, clientY: 120 }] });
    fireEvent.touchEnd(root, { changedTouches: [{ clientX: 260, clientY: 124 }] });
    expect(useSettingsStore.getState().rightPanelOpen).toBe(true);

    fireEvent.touchStart(root, { touches: [{ clientX: 120, clientY: 120 }] });
    fireEvent.touchEnd(root, { changedTouches: [{ clientX: 220, clientY: 124 }] });
    expect(useSettingsStore.getState().rightPanelOpen).toBe(false);
  });
});
