// 没有开关的一组配置，曾经拿勾不动的复选框当标题：整块淡成灰的，
// 看着像被禁用，其实里面的输入框改得动 —— 于是没人去改。
import { cleanup, render, screen, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

import { Fixed, Option } from '../components/settings/pages/settingsControls';

const CONSOLE_PAGES = ['PersonaPage', 'RuntimePage', 'StickersPage', 'TimingPage'];

afterEach(cleanup);

describe('常开配置组', () => {
  it('不画复选框 —— 画了就是在说这项可以关', () => {
    render(<Fixed name="长度上限"><input aria-label="单条消息" defaultValue="5000" /></Fixed>);

    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.getByText('常开')).toBeInTheDocument();
  });

  it('里面的输入框是能改的，也不该被淡化', () => {
    const { container } = render(
      <Fixed name="长度上限"><input aria-label="单条消息" defaultValue="5000" /></Fixed>,
    );

    expect(screen.getByLabelText('单条消息')).toBeEnabled();
    expect(container.querySelector('.opacity-50')).toBeNull();
  });

  it('标题和说明照常显示', () => {
    render(<Fixed desc="超长的回复会被截断。" name="长度上限" />);

    expect(screen.getByText('长度上限')).toBeInTheDocument();
    expect(screen.getByText('超长的回复会被截断。')).toBeInTheDocument();
  });
});

describe('普通开关', () => {
  it('还是一个能点的复选框', () => {
    render(<Option checked={false} name="合并发图" onChange={() => undefined} />);

    expect(screen.getByRole('checkbox')).toBeEnabled();
  });

  it('不再接受 disabled —— 它唯一的用法就是冒充常开组', () => {
    // 传了也不会淡化整块；类型上这个属性已经没了，这里守的是运行时行为。
    const { container } = render(
      // @ts-expect-error 故意传一个已经不存在的属性
      <Option checked disabled name="长度上限" onChange={() => undefined} />,
    );

    expect(container.querySelector('.opacity-50')).toBeNull();
    expect(screen.getByRole('checkbox')).toBeEnabled();
  });
});

describe('控制台各页', () => {
  it.each(CONSOLE_PAGES)('%s 里不再有勾不动的伪开关', (page) => {
    const source = readFileSync(join(__dirname, '..', 'console', `${page}.tsx`), 'utf-8');

    expect(source).not.toContain('checked disabled');
    expect(source).not.toContain('onChange={() => undefined}');
  });

  it('同一行里带开关和不带开关的卡片左边缘对齐', () => {
    // Fixed 里那个占位撑的就是复选框的宽度，去掉的话标题会比邻居左移一截。
    const { container } = render(<Fixed name="长度上限" />);

    const placeholder = container.querySelector('[aria-hidden]');
    expect(placeholder).toHaveClass('w-[15px]');
  });
});

describe('说明文字的缩进', () => {
  it('和带开关那种对齐到同一列', () => {
    const { container: fixed } = render(<Fixed desc="a" name="x" />);
    const fixedDesc = within(fixed as HTMLElement).getByText('a');
    cleanup();
    const { container: option } = render(
      <Option checked={false} desc="a" name="x" onChange={() => undefined} />,
    );
    const optionDesc = within(option as HTMLElement).getByText('a');

    expect(fixedDesc.className).toContain('ml-[25px]');
    expect(optionDesc.className).toContain('ml-[25px]');
  });
});
