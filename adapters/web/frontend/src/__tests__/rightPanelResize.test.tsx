// 右栏宽度。以前写死 288px 又套着 overflow-hidden，结构化编辑里的长提示词根本没法看。
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { AppLayout } from '../components/layout/AppLayout';
import {
  RIGHT_PANEL_DEFAULT_WIDTH,
  RIGHT_PANEL_MAX_WIDTH,
  RIGHT_PANEL_MIN_WIDTH,
  clampRightPanelWidth,
  useClientPrefsStore,
} from '../store/clientPrefsStore';
import { useSettingsStore } from '../store/settingsStore';

const renderLayout = () => render(
  <AppLayout header={<span>头部</span>} rightPanel={<span>面板内容</span>} sidebar={<span>侧栏</span>}>
    <span>主区</span>
  </AppLayout>,
);

const handle = () => screen.getByRole('separator', { name: '调整右侧面板宽度' });
const root = () => screen.getByTestId('app-layout-root');

beforeEach(() => {
  localStorage.clear();
  useClientPrefsStore.getState().resetClientPrefs();
  useSettingsStore.setState({ rightPanelOpen: true });
});

describe('宽度取值', () => {
  it('夹在上下限之间', () => {
    expect(clampRightPanelWidth(10)).toBe(RIGHT_PANEL_MIN_WIDTH);
    expect(clampRightPanelWidth(99999)).toBe(RIGHT_PANEL_MAX_WIDTH);
    expect(clampRightPanelWidth(400)).toBe(400);
  });

  it('坏值回到默认，不会把面板算没', () => {
    expect(clampRightPanelWidth(Number.NaN)).toBe(RIGHT_PANEL_DEFAULT_WIDTH);
  });
});

describe('拖拽把手', () => {
  it('宽度通过 CSS 变量下发，不写死在类名里', () => {
    renderLayout();

    expect(root().style.getPropertyValue('--duties-right-w')).toBe(`${RIGHT_PANEL_DEFAULT_WIDTH}px`);
  });

  it('收起面板时没有把手可拖', () => {
    useSettingsStore.setState({ rightPanelOpen: false });
    renderLayout();

    expect(screen.queryByRole('separator')).not.toBeInTheDocument();
  });

  it('把当前宽度报给读屏', () => {
    renderLayout();

    expect(handle()).toHaveAttribute('aria-valuenow', String(RIGHT_PANEL_DEFAULT_WIDTH));
    expect(handle()).toHaveAttribute('aria-valuemin', String(RIGHT_PANEL_MIN_WIDTH));
    expect(handle()).toHaveAttribute('aria-valuemax', String(RIGHT_PANEL_MAX_WIDTH));
  });

  it('左右方向键能调宽调窄', () => {
    renderLayout();

    fireEvent.keyDown(handle(), { key: 'ArrowLeft' });
    expect(useClientPrefsStore.getState().rightPanelWidth).toBe(RIGHT_PANEL_DEFAULT_WIDTH + 16);

    fireEvent.keyDown(handle(), { key: 'ArrowRight' });
    expect(useClientPrefsStore.getState().rightPanelWidth).toBe(RIGHT_PANEL_DEFAULT_WIDTH);
  });

  it('双击复位', () => {
    useClientPrefsStore.getState().setRightPanelWidth(700);
    renderLayout();

    fireEvent.doubleClick(handle());

    expect(useClientPrefsStore.getState().rightPanelWidth).toBe(RIGHT_PANEL_DEFAULT_WIDTH);
  });
});

describe('宽度记住了', () => {
  it('写进 localStorage，换一次会话还在', () => {
    renderLayout();

    fireEvent.keyDown(handle(), { key: 'ArrowLeft' });

    expect(localStorage.getItem('clonoth_client_prefs')).toContain('rightPanelWidth');
    expect(JSON.parse(localStorage.getItem('clonoth_client_prefs')!).rightPanelWidth)
      .toBe(RIGHT_PANEL_DEFAULT_WIDTH + 16);
  });

  it('存过的宽度渲染时用得上', () => {
    useClientPrefsStore.getState().setRightPanelWidth(520);
    renderLayout();

    expect(root().style.getPropertyValue('--duties-right-w')).toBe('520px');
  });
});
