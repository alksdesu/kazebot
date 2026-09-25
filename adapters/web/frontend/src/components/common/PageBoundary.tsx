import { Component, type ReactNode } from 'react';
import { Button } from './Button';

interface PageBoundaryProps {
  children: ReactNode;
  label: string;
  resetKey: string;
}

interface PageBoundaryState {
  failed: boolean;
  resetKey: string;
}

export class PageBoundary extends Component<PageBoundaryProps, PageBoundaryState> {
  state: PageBoundaryState = { failed: false, resetKey: this.props.resetKey };

  static getDerivedStateFromError() { return { failed: true }; }

  static getDerivedStateFromProps(props: PageBoundaryProps, state: PageBoundaryState) {
    return props.resetKey === state.resetKey ? null : { failed: false, resetKey: props.resetKey };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return <section className="min-w-0 space-y-4 p-4 text-sm" aria-label={`${this.props.label}加载失败`}>
      <div role="alert" className="space-y-2">
        <h2 className="text-base font-semibold">{this.props.label}暂时无法显示</h2>
        <p className="leading-relaxed text-[var(--duties-secondary)]">你仍可使用导航切换页面。请先核对已提交操作的状态；尚未保存的内容可能需要重新输入。</p>
      </div>
      <Button onClick={() => this.setState({ failed: false })}>重新加载此区域</Button>
    </section>;
  }
}
