// 页面上的交代：做成的结果和出错的原因分开显示，只做成一半也拆开说。结果和读取失败也分开记，后台重读一成功不会把刚保存的结果冲掉。
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { usePageNotice } from '../hooks/usePageNotice';

describe('usePageNotice', () => {
  it('一开始什么都不说', () => {
    const { result } = renderHook(() => usePageNotice());

    expect(result.current.notice).toBe('');
    expect(result.current.error).toBe('');
  });

  it('操作成功和读取失败分开显示，不拼成一句', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('技能已创建，请在右栏编辑 frontmatter 和正文。'); });
    act(() => { result.current.setLoadError('503 拉不到技能清单'); });

    expect(result.current.notice).toBe('技能已创建，请在右栏编辑 frontmatter 和正文。');
    expect(result.current.error).toBe('503 拉不到技能清单');
  });

  it('操作失败归到报错里，不当成提示', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setError('创建技能失败：409 已存在'); });

    expect(result.current.error).toBe('创建技能失败：409 已存在');
    expect(result.current.notice).toBe('');
  });

  it('操作失败和读取失败合成一句报错，操作失败在前', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 拉不到技能清单'); });
    act(() => { result.current.setError('删除技能失败'); });

    expect(result.current.error).toBe('删除技能失败。503 拉不到技能清单');
  });

  it('读成功只去掉读取失败，操作结果留着', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('技能已创建，请在右栏编辑 frontmatter 和正文。'); });
    act(() => { result.current.setLoadError('503 拉不到技能清单'); });
    act(() => { result.current.setLoadError(''); });

    expect(result.current.notice).toBe('技能已创建，请在右栏编辑 frontmatter 和正文。');
    expect(result.current.error).toBe('');
  });

  it('读成功不抹掉操作失败', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setError('删除技能失败'); });
    act(() => { result.current.setLoadError(''); });

    expect(result.current.error).toBe('删除技能失败');
  });

  it('换一次操作结果不碰读取失败', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 拉不到技能清单'); });
    act(() => { result.current.setMessage('技能已删除'); });

    expect(result.current.notice).toBe('技能已删除');
    expect(result.current.error).toBe('503 拉不到技能清单');
  });

  it('一次操作只有一个结果：后记的换掉先记的，不论成败', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setError('删除技能失败'); });
    act(() => { result.current.setMessage('技能已删除'); });
    expect(result.current).toMatchObject({ notice: '技能已删除', error: '' });

    act(() => { result.current.setError('创建技能失败'); });
    expect(result.current).toMatchObject({ notice: '', error: '创建技能失败' });
  });

  it('只做成一半：做成的照常交代，没做成的那一半单独报错', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('策略已保存。', '代码目录写权限没能启用：503 宿主机拒绝了'); });

    expect(result.current).toMatchObject({ notice: '策略已保存。', error: '代码目录写权限没能启用：503 宿主机拒绝了' });
  });

  it('没做成的那一半排在读取失败的原因前面', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 读不回策略'); });
    act(() => { result.current.setMessage('策略已保存。', '代码目录写权限没能启用'); });

    expect(result.current.error).toBe('代码目录写权限没能启用。503 读不回策略');
  });

  it('下一次操作的结果把做成一半的那次整个换掉，两半都不留', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('策略已保存。', '代码目录写权限没能启用'); });
    act(() => { result.current.setMessage('已保存，立即生效，不用重启'); });
    expect(result.current).toMatchObject({ notice: '已保存，立即生效，不用重启', error: '' });

    act(() => { result.current.setMessage('策略已保存。', '代码目录写权限没能启用'); });
    act(() => { result.current.setError('保存失败'); });
    expect(result.current).toMatchObject({ notice: '', error: '保存失败' });
  });

  it('读成功不抹掉没做成的那一半', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('策略已保存。', '代码目录写权限没能启用'); });
    act(() => { result.current.setLoadError(''); });

    expect(result.current).toMatchObject({ notice: '策略已保存。', error: '代码目录写权限没能启用' });
  });

  it('记一个空结果就把上一次操作的结果清掉，读取失败不动', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 拉不到技能清单'); });
    act(() => { result.current.setError('删除技能失败'); });
    act(() => { result.current.setMessage(''); });

    expect(result.current).toMatchObject({ notice: '', error: '503 拉不到技能清单' });
  });

  it('手动刷新全清', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('技能已删除'); });
    act(() => { result.current.setLoadError('503 拉不到技能清单'); });
    act(() => { result.current.clearNotice(); });

    expect(result.current).toMatchObject({ notice: '', error: '' });

    act(() => { result.current.setError('删除技能失败'); });
    act(() => { result.current.clearNotice(); });

    expect(result.current.error).toBe('');
  });

  it('操作失败和读取失败说的是同一句就只说一遍', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setError('503 拉不到技能清单'); });
    act(() => { result.current.setLoadError('503 拉不到技能清单'); });

    expect(result.current.error).toBe('503 拉不到技能清单');
  });

  it('四个写入口的引用一直不变', () => {
    // 各页把它们摆进 effect 的依赖里，换引用就会让重读、轮询无谓地重挂一遍。
    const { result } = renderHook(() => usePageNotice());
    const first = result.current;

    act(() => { result.current.setMessage('技能已删除'); });
    act(() => { result.current.setError('删除技能失败'); });

    expect(result.current.setMessage).toBe(first.setMessage);
    expect(result.current.setError).toBe(first.setError);
    expect(result.current.setLoadError).toBe(first.setLoadError);
    expect(result.current.clearNotice).toBe(first.clearNotice);
  });

  it('同一个结果再记一遍不重渲染', () => {
    // 没重渲染时 result.current 还是上一次那个对象。
    const { result } = renderHook(() => usePageNotice());
    const fresh = result.current;

    act(() => { result.current.setMessage(''); });
    expect(result.current).toBe(fresh);

    act(() => { result.current.setError('删除技能失败'); });
    const settled = result.current;
    act(() => { result.current.setError('删除技能失败'); });
    expect(result.current).toBe(settled);

    act(() => { result.current.setMessage('策略已保存。', '代码目录写权限没能启用'); });
    const half = result.current;
    act(() => { result.current.setMessage('策略已保存。', '代码目录写权限没能启用'); });
    expect(result.current).toBe(half);
  });

  it('两路各记各的，一路读成功不抹掉另一路的失败', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 总览读不回来', 'overview'); });
    act(() => { result.current.setLoadError('500 条目读不回来', 'entries'); });
    act(() => { result.current.setLoadError('', 'entries'); });

    expect(result.current.error).toBe('503 总览读不回来');
  });

  it('几路的失败按写下的先后接在操作失败后面', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 总览读不回来', 'overview'); });
    act(() => { result.current.setLoadError('500 条目读不回来', 'entries'); });
    act(() => { result.current.setError('添加失败'); });

    expect(result.current.error).toBe('添加失败。503 总览读不回来。500 条目读不回来');
  });

  it('两路报的是同一句就只说一遍', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('503 后台连不上', 'overview'); });
    act(() => { result.current.setLoadError('503 后台连不上', 'entries'); });

    expect(result.current.error).toBe('503 后台连不上');
  });

  it('手动刷新把几路的失败一起清掉', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setMessage('已添加'); });
    act(() => { result.current.setLoadError('503 总览读不回来', 'overview'); });
    act(() => { result.current.setLoadError('500 条目读不回来', 'entries'); });
    act(() => { result.current.clearNotice(); });

    expect(result.current).toMatchObject({ notice: '', error: '' });
  });

  it('分开记之后写入口的引用照样不变', () => {
    const { result } = renderHook(() => usePageNotice());
    const first = result.current;

    act(() => { result.current.setLoadError('503 总览读不回来', 'overview'); });
    act(() => { result.current.setLoadError('500 条目读不回来', 'entries'); });

    expect(result.current.setMessage).toBe(first.setMessage);
    expect(result.current.setError).toBe(first.setError);
    expect(result.current.setLoadError).toBe(first.setLoadError);
    expect(result.current.clearNotice).toBe(first.clearNotice);
  });

  it('同一路重复报同一句不重渲染', () => {
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
    expect(result.current.error).toBe('');
  });

  it('头一回报空之后，真出了错照样报得出来', () => {
    const { result } = renderHook(() => usePageNotice());

    act(() => { result.current.setLoadError('', 'entries'); });
    act(() => { result.current.setLoadError('500 条目读不回来', 'entries'); });

    expect(result.current.error).toBe('500 条目读不回来');
  });
});
