import { readFileSync } from 'node:fs';
import { createRef } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Button, Check, ErrorText, Facts, Grid, Input, LinkButton, PageShell, SaveBar, Segmented, Select, StatusBar, Textarea } from '../components/settings/pages/settingsControls';

describe('设置共享控件的可读与交互样式合同', () => {
  it('输入控件复用表单token、保留ref与事件，不强制中文使用等宽字体', () => {
    const ref = createRef<HTMLInputElement>(); const change = vi.fn();
    render(<><Input aria-label="模型别名" ref={ref} onChange={change} /><Select aria-label="接口"><option>原生</option></Select><Textarea aria-label="说明" /></>);
    for (const control of [screen.getByLabelText('模型别名'), screen.getByLabelText('接口'), screen.getByLabelText('说明')]) {
      expect(control).toHaveClass('app-input', 'min-w-0', 'max-w-full'); expect(control).not.toHaveClass('font-mono', 'text-xs', 'outline-none');
    }
    expect(ref.current).toBe(screen.getByLabelText('模型别名'));
    fireEvent.change(ref.current!, { target: { value: '中文别名' } }); expect(change).toHaveBeenCalledOnce();
  });
  it('默认、小号、链接与分段选项使用相同最小点击高度和清晰字号', () => {
    const pick = vi.fn(); render(<><Button>默认按钮</Button><Button size="sm" tone="danger">删除条目</Button><LinkButton href="#example">链接操作</LinkButton><Segmented label="调用模式" choices={[[ 'native', '原生工具调用' ], [ 'json', 'JSON 格式' ]]} value="native" onPick={pick} /></>);
    for (const control of [...screen.getAllByRole('button'), screen.getByRole('link')]) {
      expect(control).toHaveClass('app-button', 'text-sm'); expect(control).not.toHaveClass('font-mono');
      expect(control.className.includes('min-h-[var(--duties-control-size)]') || control.classList.contains('settings-segment')).toBe(true);
    }
    expect(screen.getByRole('button', { name: '原生工具调用' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'JSON 格式' })); expect(pick).toHaveBeenCalledWith('json');
  });
  it('保存忙态与禁用分段继续阻止操作，复选框保持原有事件语义', () => {
    const action = vi.fn(); render(<><SaveBar busy dirty note="正在提交" onSave={action} onReset={action} /><Segmented disabled choices={[[ 'one', '一档' ]]} value="one" onPick={action} /><Check checked={false} onChange={action}>使用默认值</Check></>);
    fireEvent.click(screen.getByRole('button', { name: '正在保存…' })); fireEvent.click(screen.getByRole('button', { name: '丢弃' })); fireEvent.click(screen.getByRole('button', { name: '一档' })); expect(action).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('checkbox', { name: '使用默认值' })); expect(action).toHaveBeenCalledWith(true);
  });
  it('状态条只播报会变的那句，靠右的按钮不进播报区', () => {
    render(<StatusBar status="配置已生效" tone="live"><Button>应用</Button></StatusBar>);
    const live = screen.getByRole('status');
    expect(live).toHaveAttribute('aria-live', 'polite');
    expect(live.textContent).toBe('配置已生效');
    expect(within(live).queryByRole('button')).toBeNull();
    expect(screen.getByRole('button', { name: '应用' })).toBeInTheDocument();
  });
  it('保存结果那句读屏会念出来', () => {
    render(<SaveBar busy={false} dirty note="已保存，下一条消息就会用新的人设。" onReset={vi.fn()} onSave={vi.fn()} />);
    const note = screen.getByRole('status');
    expect(note).toHaveAttribute('aria-live', 'polite');
    expect(note.textContent).toBe('已保存，下一条消息就会用新的人设。');
  });
  it('错误与交代只在显式要求时播报，静态说明不打断读屏', () => {
    const { rerender } = render(<><ErrorText>还没有 _persona.md</ErrorText><Facts>任意模型 id 都能填</Facts></>);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByText('任意模型 id 都能填')).not.toHaveAttribute('aria-live');
    rerender(<><ErrorText role="alert">还没有 _persona.md</ErrorText><Facts role="status">任意模型 id 都能填</Facts></>);
    expect(screen.getByRole('alert').textContent).toBe('还没有 _persona.md');
    const facts = screen.getByRole('status');
    expect(facts).toHaveAttribute('aria-live', 'polite');
    expect(facts.textContent).toBe('任意模型 id 都能填');
  });
  it('栅格最小宽度不超过容器，分段按完整词换行而不是缩字', () => {
    const { container } = render(<Grid><span>内容</span></Grid>);
    expect(container.firstChild).toHaveClass('settings-control-grid', 'min-w-0');
    const css = readFileSync('src/styles/index.css', 'utf8');
    expect(css).toContain('minmax(min(100%, var(--duties-control-column-min)), 1fr)');
    expect(css).toMatch(/\.settings-segment\s*\{[^}]*min-height:\s*var\(--duties-control-size\)[^}]*word-break:\s*keep-all/s);
    expect(css).toMatch(/--duties-control-size:\s*2\.5rem/); expect(css).toMatch(/--duties-font-size-body:\s*0\.875rem/);
  });
});
