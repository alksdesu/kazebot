import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { PageHeader } from '../components/settings/pages/settingsPagePrimitives';
import { FeaturePage } from '../features/ui';

afterEach(() => cleanup());

describe('工作区标题层级', () => {
  it('独立页面保留主标题', () => {
    render(<FeaturePage title="任务" description="核对计划后执行。"><p>页面内容</p></FeaturePage>);
    expect(screen.getByRole('heading', { level: 1, name: '任务' })).toBeVisible();
  });

  it('嵌入功能使用二级标题，不与宿主页面争用主标题', () => {
    render(<><PageHeader title="自动化" description="管理定时任务。" /><FeaturePage embedded title="提醒与待办" description="管理提醒。"><p>提醒内容</p></FeaturePage></>);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('heading', { level: 2, name: '提醒与待办' })).toBeVisible();
  });
});
