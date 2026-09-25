import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api/supervisorClient';
import { Button, Modal } from '../components/common';
import { AppLayout, Header, Sidebar } from '../components/layout';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { useClientPrefsStore } from '../store/clientPrefsStore';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';

vi.mock('../api/supervisorClient', async original => ({
  ...await original<typeof import('../api/supervisorClient')>(),
  getActiveNode: vi.fn(), getAppConfig: vi.fn(), getNodes: vi.fn(), getSessionProviderOverride: vi.fn(),
}));

const viewport = (width: number) => { Object.defineProperty(window, 'innerWidth', { configurable: true, value: width }); fireEvent(window, new Event('resize')); };
const layout = (rail = true, navigationKey = 'one') => <AppLayout navigationKey={navigationKey} header="页面" sidebar={<button>导航目标</button>} rightPanel={rail ? <button>详情操作</button> : undefined}><button>主区操作</button></AppLayout>;

beforeEach(() => {
  viewport(1440); localStorage.clear(); useClientPrefsStore.getState().resetClientPrefs();
  useViewStore.setState({ viewMode: 'chat', activeSettingsTab: 'general' });
  useSettingsStore.setState({ rightPanelOpen: true, adminToken: null, activeNodeId: '', activeNodeSessionId: '', entryNodeId: '', availableNodes: [], sessionProviderOverride: null, sessionProviderOverrideSessionId: '' });
  vi.mocked(api.getActiveNode).mockResolvedValue({ node_id: 'main', is_override: false, default_node_id: 'main' });
  vi.mocked(api.getAppConfig).mockResolvedValue({ provider: 'openai', openai: {} } as never);
  vi.mocked(api.getSessionProviderOverride).mockResolvedValue({});
  vi.mocked(api.getNodes).mockResolvedValue([]);
});
afterEach(() => { vi.restoreAllMocks(); });

describe('可访问的弹层与响应式壳', () => {
  it('没有右栏的视图在缩窄窗口后不会遗留遮罩', () => {
    render(layout(false)); act(() => viewport(390));
    expect(screen.queryByTestId('panel-backdrop')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('右侧面板')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '主区操作' })).toBeVisible();
  });
  it('平板右栏按需覆盖并恢复焦点，Escape只关闭当前面板', async () => {
    viewport(1024); render(layout());
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    const trigger = screen.getByRole('button', { name: '展开面板' }); trigger.focus(); fireEvent.click(trigger);
    expect(screen.getByRole('dialog', { name: '右侧面板' })).toContainElement(document.activeElement as HTMLElement);
    expect(document.getElementById('app-main')).toHaveAttribute('inert');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus()); expect(document.getElementById('app-main')).not.toHaveAttribute('inert');
  });
  it('移动导航仅在打开后可聚焦，Tab在抽屉内循环，导航变化后关闭', async () => {
    viewport(390); const view = render(layout());
    expect(screen.queryByRole('button', { name: '导航目标' })).not.toBeInTheDocument();
    const menu = screen.getByRole('button', { name: '打开导航' }); menu.focus(); fireEvent.click(menu);
    const target = screen.getByRole('button', { name: '导航目标' }); target.focus(); fireEvent.keyDown(document, { key: 'Tab' });
    expect(screen.getByRole('button', { name: '关闭导航' })).toHaveFocus();
    view.rerender(layout(true, 'two'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); await waitFor(() => expect(menu).toHaveFocus());
  });
  it('按视口限制有效宽度但不损坏保存的宽度偏好', () => {
    useClientPrefsStore.getState().setRightPanelWidth(880); render(layout());
    expect(screen.getByTestId('app-layout-root').style.getPropertyValue('--duties-right-w')).toBe('716px');
    expect(screen.getByRole('separator')).toHaveAttribute('aria-valuemax', '716');
    expect(useClientPrefsStore.getState().rightPanelWidth).toBe(880);
    act(() => viewport(1680)); expect(screen.getByTestId('app-layout-root').style.getPropertyValue('--duties-right-w')).toBe('880px');
  });
  it('取消拖拽后后续指针移动不再改变面板', () => {
    render(layout()); const handle = screen.getByRole('separator');
    fireEvent(handle, new MouseEvent('pointerdown', { bubbles: true, button: 0, clientX: 500 }));
    fireEvent(window, new MouseEvent('pointermove', { clientX: 450 }));
    expect(screen.getByTestId('app-layout-root').style.getPropertyValue('--duties-right-w')).toBe('338px');
    fireEvent(window, new Event('pointercancel')); fireEvent(window, new MouseEvent('pointermove', { clientX: 400 }));
    expect(screen.getByTestId('app-layout-root').style.getPropertyValue('--duties-right-w')).toBe('288px');
  });
  it('模态焦点进入、循环并关闭后回到发起按钮', async () => {
    function Example() { const [open, setOpen] = useState(false); return <><button onClick={() => setOpen(true)}>打开</button><Modal open={open} onClose={() => setOpen(false)} title="示例"><input aria-label="编辑" /></Modal></>; }
    render(<Example />); const launcher = screen.getByRole('button', { name: '打开' }); launcher.focus(); fireEvent.click(launcher);
    const close = screen.getByRole('button', { name: '关闭示例' }); expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true }); expect(screen.getByRole('textbox')).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Tab' }); expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Escape' }); await waitFor(() => expect(launcher).toHaveFocus());
  });
  it('嵌套弹层只关闭最上层，同时卸载不遗留inert', () => {
    function Example() { const [inner, setInner] = useState(false); return <Modal open title="外层" onClose={() => {}}><button onClick={() => setInner(true)}>打开内层</button><Modal open={inner} title="内层" onClose={() => setInner(false)}><input /></Modal></Modal>; }
    const view = render(<Example />); fireEvent.click(screen.getByRole('button', { name: '打开内层' }));
    fireEvent.keyDown(document, { key: 'Escape' }); expect(screen.getByRole('dialog', { name: '外层' })).toBeInTheDocument(); expect(screen.queryByRole('dialog', { name: '内层' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打开内层' })); view.unmount();
    expect(document.querySelector('[inert]')).toBeNull(); expect(document.body.style.overflow).not.toBe('hidden');
  });
  it.each(['hidden-parent', 'closed-details', 'disabled-fieldset'])('焦点循环忽略实际不可聚焦的后代：%s', kind => {
    render(<Modal open title="可见性" onClose={() => {}}>
      <button>有效末项</button>
      {kind === 'hidden-parent' && <div style={{ display: 'none' }}><input aria-label="隐藏输入" data-autofocus /></div>}
      {kind === 'closed-details' && <details><summary>可见摘要</summary><button data-autofocus>关闭详情中的按钮</button></details>}
      {kind === 'disabled-fieldset' && <fieldset disabled><input aria-label="禁用输入" data-autofocus /></fieldset>}
    </Modal>);
    const close = screen.getByRole('button', { name: '关闭可见性' }); expect(close).toHaveFocus();
    const last = kind === 'closed-details' ? screen.getByText('可见摘要') : screen.getByRole('button', { name: '有效末项' });
    last.focus(); fireEvent.keyDown(document, { key: 'Tab' }); expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true }); expect(last).toHaveFocus();
  });
  it('发起按钮被导航卸载后回焦到新的主区', async () => {
    function Example() {
      const [open, setOpen] = useState(false); const [navigated, setNavigated] = useState(false);
      return <><main id="app-main" tabIndex={-1}>{!navigated && <button onClick={() => setOpen(true)}>打开旧页面</button>}</main>
        <Modal open={open} title="导航" onClose={() => setOpen(false)}><button onClick={() => { setNavigated(true); setOpen(false); }}>前往新页面</button></Modal></>;
    }
    render(<Example />); const launcher = screen.getByRole('button', { name: '打开旧页面' }); launcher.focus(); fireEvent.click(launcher);
    fireEvent.click(screen.getByRole('button', { name: '前往新页面' }));
    await waitFor(() => expect(screen.getByRole('main')).toHaveFocus());
  });
  it('正在操作的共享按钮不可重复触发并保留可及名称', () => {
    const click = vi.fn(); render(<Button loading onClick={click}>保存配置</Button>);
    const button = screen.getByRole('button', { name: '保存配置' }); expect(button).toBeDisabled(); expect(button).toHaveAttribute('aria-busy', 'true'); fireEvent.click(button); expect(click).not.toHaveBeenCalled();
  });
});

describe('会话绑定与草稿导航', () => {
  it('迟到的A节点响应不能覆盖已经切换到B的节点', async () => {
    let finishA!: (value: Awaited<ReturnType<typeof api.getActiveNode>>) => void;
    vi.mocked(api.getActiveNode).mockImplementation(id => id === 'A' ? new Promise(resolve => { finishA = resolve; }) : Promise.resolve({ node_id: 'node-B', is_override: true, default_node_id: 'main' }));
    const view = render(<Header sessionId="A" title="A" isGenerating={false} />); view.rerender(<Header sessionId="B" title="B" isGenerating={false} />);
    await waitFor(() => expect(useSettingsStore.getState().activeNodeId).toBe('node-B'));
    await act(async () => finishA({ node_id: 'node-A', is_override: true, default_node_id: 'main' }));
    expect(useSettingsStore.getState().activeNodeId).toBe('node-B'); expect(useSettingsStore.getState().activeNodeSessionId).toBe('B');
    screen.getByTitle('切换节点').focus(); expect(screen.getByTitle('切换节点')).toHaveFocus(); expect(screen.getByTitle('切换节点').tagName).toBe('BUTTON');
  });
  it('子会话查看不暴露父会话取消或重置动作', () => {
    const view = render(<Header sessionId="child" title="child" isGenerating viewingChildNodeId="child-node" onCancel={vi.fn()} onReset={vi.fn()} />);
    expect(screen.queryByRole('button', { name: '取消' })).not.toBeInTheDocument();
    view.rerender(<Header sessionId="child" title="child" isGenerating={false} viewingChildNodeId="child-node" onReset={vi.fn()} />);
    expect(screen.queryByRole('button', { name: '重置对话' })).not.toBeInTheDocument();
  });
  it('新对话标题栏不显示上一会话绑定的节点', () => {
    useSettingsStore.setState({ entryNodeId: 'entry', activeNodeId: 'old-node', activeNodeSessionId: 'old-session' });
    render(<Header sessionId="no-session" title="新对话" isGenerating={false} />);
    expect(screen.getByTitle('切换节点')).toHaveTextContent('entry'); expect(screen.getByTitle('切换节点')).not.toHaveTextContent('old-node');
  });
  it('删除先确认，失败有反馈且不误报成功', async () => {
    const remove = vi.fn().mockRejectedValue(new Error('synthetic delete failure')); const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<Sidebar conversations={[{ id: 'A', title: '测试对话', sessionId: 'sid', updatedAt: 'not-a-date' }]} activeConversationId="A" onCreateConversation={() => {}} onSelectConversation={() => {}} onDeleteConversation={remove} />);
    const button = screen.getByRole('button', { name: '删除对话 测试对话' });
    fireEvent.click(button); expect(remove).not.toHaveBeenCalled(); expect(confirm).toHaveBeenCalledWith(expect.stringContaining('测试对话'));
    confirm.mockReturnValue(true); fireEvent.click(button); expect(button).toBeDisabled();
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('synthetic delete failure'));
    expect(button).toBeEnabled(); expect(screen.getByText('时间未知')).toBeInTheDocument();
  });
  it('离开脏草稿先确认，拒绝不改视图，卸载移除保护', () => {
    function Draft() { useUnsavedChanges(true); return <input aria-label="草稿" />; }
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false); const view = render(<Draft />);
    act(() => useViewStore.getState().openWorkspace('materials')); expect(useViewStore.getState().viewMode).toBe('chat'); expect(confirm).toHaveBeenCalledOnce();
    confirm.mockReturnValue(true); act(() => useViewStore.getState().openWorkspace('materials')); expect(useViewStore.getState().viewMode).toBe('materials');
    view.unmount(); confirm.mockClear(); act(() => useViewStore.getState().openWorkspace('chat')); expect(confirm).not.toHaveBeenCalled();
  });
  it('重新进入设置保留上次分类', () => {
    act(() => useViewStore.getState().openSettings('tools')); act(() => useViewStore.getState().closeSettings()); act(() => useViewStore.getState().openSettings());
    expect(useViewStore.getState().activeSettingsTab).toBe('tools');
  });
});
