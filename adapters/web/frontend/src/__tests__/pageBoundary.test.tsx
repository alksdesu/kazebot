import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PageBoundary } from '../components/common/PageBoundary';

let broken = false;
function Content() {
  if (broken) throw new Error('synthetic-private-error-detail');
  return <p>区域已就绪</p>;
}

beforeEach(() => { broken = false; vi.spyOn(console, 'error').mockImplementation(() => {}); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('页面故障隔离', () => {
  it('正常页面不增加可见包装或改变内容', () => {
    render(<PageBoundary label="任务" resetKey="tasks"><Content /></PageBoundary>);
    expect(screen.getByText('区域已就绪')).toBeVisible();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('只隔离失败区域，不显示原始错误，重试可恢复', () => {
    broken = true;
    render(<><nav>导航仍可用</nav><PageBoundary label="任务" resetKey="tasks"><Content /></PageBoundary></>);
    expect(screen.getByRole('alert')).toHaveTextContent('任务暂时无法显示');
    expect(screen.getByText('导航仍可用')).toBeVisible();
    expect(screen.queryByText('synthetic-private-error-detail')).toBeNull();
    broken = false;
    fireEvent.click(screen.getByRole('button', { name: '重新加载此区域' }));
    expect(screen.getByText('区域已就绪')).toBeVisible();
  });

  it('切换页面身份后自动解除旧区域错误状态', () => {
    broken = true;
    const view = render(<PageBoundary label="任务" resetKey="tasks"><Content /></PageBoundary>);
    broken = false;
    view.rerender(<PageBoundary label="资料" resetKey="materials"><Content /></PageBoundary>);
    expect(screen.getByText('区域已就绪')).toBeVisible();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('持续失败时保持恢复入口，不无限自动重试', () => {
    broken = true;
    render(<PageBoundary label="详情" resetKey="details"><Content /></PageBoundary>);
    fireEvent.click(screen.getByRole('button', { name: '重新加载此区域' }));
    expect(screen.getByRole('alert')).toHaveTextContent('详情暂时无法显示');
    expect(screen.getByRole('button', { name: '重新加载此区域' })).toBeEnabled();
  });
});
