// 页面上那一句提示：操作结果和读取失败分开记。混成一个 state 的话，后台重读一成功就把刚保存的结果冲掉了。
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { usePageNotice } from '../hooks/usePageNotice';

describe('usePageNotice', () => {
  it('一开始什么都不说', () => {
    const { result } = renderHook(() => usePageNotice());

    expect(result.current.notice).toBe('');
  });

  it('操作结果和读取失败合成一句', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('技能已创建，请在右栏编辑 frontmatter 和正文。'); });
    act(() => { result.current.setLoadError('503 拉不到技能清单'); });

    expect(result.current.notice).toBe('技能已创建，请在右栏编辑 frontmatter 和正文。503 拉不到技能清单');
  });

  it('读成功只去掉读取失败那一半，操作结果留着', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('技能已创建，请在右栏编辑 frontmatter 和正文。'); });
    act(() => { result.current.setLoadError('503 拉不到技能清单'); });
    act(() => { result.current.setLoadError(''); });

    expect(result.current.notice).toBe('技能已创建，请在右栏编辑 frontmatter 和正文。');
  });

  it('换一次操作结果不碰读取失败', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 拉不到技能清单'); });
    act(() => { result.current.setMessage('技能已删除'); });

    expect(result.current.notice).toBe('技能已删除。503 拉不到技能清单');
  });

  it('手动刷新两样都清', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('技能已删除'); });
    act(() => { result.current.setLoadError('503 拉不到技能清单'); });
    act(() => { result.current.clearNotice(); });

    expect(result.current.notice).toBe('');
  });

  it('两半说的是同一句就只说一遍', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('503 拉不到技能清单'); });
    act(() => { result.current.setLoadError('503 拉不到技能清单'); });

    expect(result.current.notice).toBe('503 拉不到技能清单');
  });

  it('三个写入口的引用一直不变', () => {
    // 各页把它们摆进 effect 的依赖里，换引用就会让重读、轮询无谓地重挂一遍。
    const { result } = renderHook(() => usePageNotice());
    const first = result.current;

    act(() => { result.current.setMessage('技能已删除'); });

    expect(result.current.setMessage).toBe(first.setMessage);
    expect(result.current.setLoadError).toBe(first.setLoadError);
    expect(result.current.clearNotice).toBe(first.clearNotice);
  });
});
