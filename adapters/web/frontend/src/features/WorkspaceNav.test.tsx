import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { WorkspaceNav } from './WorkspaceNav';

const state = vi.hoisted(() => ({ viewMode: 'chat', openWorkspace: vi.fn() }));
vi.mock('../store/viewStore', () => ({ useViewStore: () => state }));

beforeEach(() => { state.viewMode = 'chat'; state.openWorkspace.mockClear(); });

describe('narrow workspace navigation', () => {
  it('stacks icons above unbroken labels in three equal columns at sidebar width', () => {
    render(<div style={{ width: 240 }}><WorkspaceNav /></div>);
    const nav = screen.getByRole('navigation', { name: '工作区' });
    expect(nav).toHaveClass('grid', 'grid-cols-3', 'min-w-0');
    for (const label of ['聊天', '任务', '资料']) {
      const button = within(nav).getByRole('button', { name: label });
      expect(button).toHaveClass('flex-col', 'min-h-14', 'text-sm');
      expect(within(button).getByText(label)).toHaveClass('whitespace-nowrap');
      expect(button.querySelector('svg')).not.toBeNull();
    }
    expect(within(nav).getAllByRole('button')).toHaveLength(3);
  });

  it('retains the active page indicator and the existing navigation action', () => {
    state.viewMode = 'materials';
    render(<WorkspaceNav />);
    expect(screen.getByRole('button', { name: '资料' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: '聊天' })).not.toHaveAttribute('aria-current');
    fireEvent.click(screen.getByRole('button', { name: '任务' }));
    expect(state.openWorkspace).toHaveBeenCalledExactlyOnceWith('execution');
  });
});
