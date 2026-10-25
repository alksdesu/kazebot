// 对话里的 Markdown：链接都在新标签页打开、不带来源；图片不自动请求，只给一个链接；原始 HTML 和 javascript: 不生效。
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Markdown } from '../components/common/Markdown';

describe('对话 Markdown 的链接与图片', () => {
  it('链接在新标签页打开，不带来源', () => {
    render(<Markdown>{'看 [文档](https://docs.example/a)'}</Markdown>);

    const link = screen.getByRole('link', { name: '文档' });
    expect(link).toHaveAttribute('href', 'https://docs.example/a');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('自动认出来的网址也一样', () => {
    render(<Markdown>{'地址 https://docs.example/b 在这'}</Markdown>);

    const link = screen.getByRole('link', { name: 'https://docs.example/b' });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  // 图片一渲染就会向对方发请求，图片地址里能拼进对话内容。
  it('图片不渲染成 img，只给一个点了才打开的链接', () => {
    const { container } = render(<Markdown>{'![猫](https://img.example/cat.png?q=secret)'}</Markdown>);

    expect(container.querySelector('img')).toBeNull();
    const link = screen.getByRole('link', { name: '图片：猫' });
    expect(link).toHaveAttribute('href', 'https://img.example/cat.png?q=secret');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('没写说明的图片用标题，标题也没有就叫未命名', () => {
    render(<Markdown>{'![](https://img.example/a.png "封面")\n\n![](https://img.example/b.png)'}</Markdown>);

    expect(screen.getByRole('link', { name: '图片：封面' })).toHaveAttribute('href', 'https://img.example/a.png');
    expect(screen.getByRole('link', { name: '图片：未命名' })).toHaveAttribute('href', 'https://img.example/b.png');
  });

  it('原始 HTML 只当文字，javascript: 链接被清掉', () => {
    const { container } = render(<Markdown>{'<img src="https://img.example/x.png">\n\n[点我](javascript:alert(1))'}</Markdown>);

    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('a[href^="javascript"]')).toBeNull();
    expect(screen.getByText('点我')).toBeInTheDocument();
  });
});
