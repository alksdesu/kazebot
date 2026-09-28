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

  it('两路各记各的，一路读成功不抹掉另一路的失败', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 总览读不回来', 'overview'); });
    act(() => { result.current.setLoadError('500 条目读不回来', 'entries'); });
    act(() => { result.current.setLoadError('', 'entries'); });

    expect(result.current.notice).toBe('503 总览读不回来');
  });

  it('几路的失败按写下的先后接在操作结果后面', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 总览读不回来', 'overview'); });
    act(() => { result.current.setLoadError('500 条目读不回来', 'entries'); });
    act(() => { result.current.setMessage('已添加'); });

    expect(result.current.notice).toBe('已添加。503 总览读不回来。500 条目读不回来');
  });

  it('两路报的是同一句就只说一遍', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 后台连不上', 'overview'); });
    act(() => { result.current.setLoadError('503 后台连不上', 'entries'); });

    expect(result.current.notice).toBe('503 后台连不上');
  });

  it('手动刷新把几路的失败一起清掉', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('已添加'); });
    act(() => { result.current.setLoadError('503 总览读不回来', 'overview'); });
    act(() => { result.current.setLoadError('500 条目读不回来', 'entries'); });
    act(() => { result.current.clearNotice(); });

    expect(result.current.notice).toBe('');
  });

  it('分开记之后三个写入口的引用照样不变', () => {
    const { result } = renderHook(() => usePageNotice());
    const first = result.current;

    act(() => { result.current.setLoadError('503 总览读不回来', 'overview'); });
    act(() => { result.current.setLoadError('500 条目读不回来', 'entries'); });

    expect(result.current.setMessage).toBe(first.setMessage);
    expect(result.current.setLoadError).toBe(first.setLoadError);
    expect(result.current.clearNotice).toBe(first.clearNotice);
  });

  it('同一路重复报同一句不重渲染', () => {
    // 没重渲染时 result.current 还是上一次那个对象。
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 总览读不回来', 'overview'); });
    const settled = result.current;
    act(() => { result.current.setLoadError('503 总览读不回来', 'overview'); });

    expect(result.current).toBe(settled);
  });

  it('轮询一路读成功，一遍遍报空也不重渲染', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('', 'overview'); });
    const settled = result.current;
    act(() => { result.current.setLoadError('', 'overview'); });

    expect(result.current).toBe(settled);
  });

  it('一路头一回就报空，也不白渲染一遍', () => {
    // 这一路还没记过任何东西，和「读成功」说的是同一件事，不该为它新建一份。
    const { result } = renderHook(() => usePageNotice());
    const fresh = result.current;

    act(() => { result.current.setLoadError('', 'entries'); });

    expect(result.current).toBe(fresh);
    expect(result.current.notice).toBe('');
  });

  it('头一回报空之后，真出了错照样报得出来', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('', 'entries'); });
    act(() => { result.current.setLoadError('500 条目读不回来', 'entries'); });

    expect(result.current.notice).toBe('500 条目读不回来');
  });
});
