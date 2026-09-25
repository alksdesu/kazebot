import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { confirmNavigation, useUnsavedChanges } from '../hooks/useUnsavedChanges';

function Draft({ dirty = true }: { dirty?: boolean }) { useUnsavedChanges(dirty, '保留未保存的实例草稿吗？'); return null; }
function unload() {
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('实例整页导航原生保护', () => {
  it('没有草稿不阻止整页导航', () => {
    render(<Draft dirty={false} />);
    expect(unload()).toBe(false);
  });

  it('脏草稿交给原生 beforeunload 确认，不叠加 window.confirm', () => {
    render(<Draft />);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    expect(unload()).toBe(true);
    expect(confirm).not.toHaveBeenCalled();
  });

  it('取消离开后仍保护下次导航，不遗留一次性放行状态', () => {
    render(<Draft />);
    expect(unload()).toBe(true);
    expect(unload()).toBe(true);
  });

  it('保存或清空草稿后取消离开保护', () => {
    const view = render(<Draft />);
    expect(unload()).toBe(true);
    view.rerender(<Draft dirty={false} />);
    expect(unload()).toBe(false);
  });

  it('实例页面卸载不会遗留旧实例的拦截', () => {
    const view = render(<Draft />);
    view.unmount();
    expect(unload()).toBe(false);
  });

  it('页内导航继续使用原来的确认逻辑，拒绝不清理草稿', () => {
    render(<Draft />);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    expect(confirmNavigation()).toBe(false);
    expect(confirm).toHaveBeenCalledExactlyOnceWith('保留未保存的实例草稿吗？');
    expect(unload()).toBe(true);
  });
});
