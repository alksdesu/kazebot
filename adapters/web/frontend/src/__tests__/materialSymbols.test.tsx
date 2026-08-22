// [2026-06-02] Material Symbols SVG migration coverage.
// Why: the shared icon primitive no longer renders font ligatures or depends on a subset woff2 file.
// How: assert the primitive emits the SVG component classes, preserves caller sizing and class names,
// and still accepts the legacy filled prop without changing the outlined W400 SVG path. Purpose: catch
// regressions that reintroduce the old .material-symbols-outlined font contract.
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Icon } from '../components/common/Icon';
import { settingsTabs } from '../components/settings/settingsTabs';

describe('Material Symbols SVG icon system', () => {
  it('renders Material Symbols through the shared SVG Icon component', () => {
    const { container } = render(<Icon name="menu" size={24} filled className="text-blue-600" />);
    const icon = container.querySelector('svg.material-symbols_menu');

    expect(icon).toBeInTheDocument();
    expect(icon).toHaveClass('material-symbols');
    expect(icon).toHaveClass('text-blue-600');
    expect(icon).toHaveAttribute('width', '24');
    expect(icon).toHaveAttribute('height', '24');
    expect(icon).toHaveStyle({ verticalAlign: 'middle' });
    expect(container.querySelector('.material-symbols-outlined')).not.toBeInTheDocument();
    expect(icon).not.toHaveTextContent('menu');
  });

  it('falls back within the icon slot for unknown names', () => {
    // 图标名可以来自工具数据，赶在 ICON_MAP 更新之前到达。缺一个图标是小事，
    // 但把整个名字按 size 吐出来会撑破那一格，跟旁边的导航文字叠在一起。
    const { container } = render(<Icon name="unknown_symbol" size={18} className="text-red-600" />);
    const fallback = container.firstElementChild as HTMLElement;

    expect(fallback).toHaveClass('text-red-600');
    expect(fallback).toHaveStyle({ width: '18px', height: '18px', overflow: 'hidden' });
    expect(fallback.textContent!.length).toBeLessThanOrEqual(2);
  });

  it('keeps the missing name findable', () => {
    // 名字不显示了，但排查的人还得知道是哪个缺了。
    const { container } = render(<Icon name="unknown_symbol" />);
    expect(container.firstElementChild).toHaveAttribute('title', 'unknown_symbol');
  });

  it('stores Material Symbol names in the settings tab registry', () => {
    // [2026-06-02] Why: the settings registry now includes all P0/P1 settings tabs.
    // How: assert every registered Material Symbol name in order instead of the old
    // four-tab subset. Purpose: icon migration coverage grows with the full settings
    // surface and catches any future literal glyph regression.
    expect(settingsTabs.map(tab => tab.icon)).toEqual([
      'tune',
      'display_settings',
      'monitor_heart',
      'settings_power',
      'approval',
      'smart_toy',
      'folder_managed',
      'build',
      'palette',
      'menu_book',
      'cable',
      'schedule',
      'code',
      'account_circle',
      'inbox',
      'timer',
      'verified_user',
      'draft',
      'psychology',
      'photo_library',
      'lan',
      'hub',
      'model_training',
    ]);

    // 注册了却没 import 过 —— ICON_MAP 落空，回退件会把名字塞进 16px 的格子里，
    // 跟旁边的导航文字叠在一起。出过一次。
    for (const tab of settingsTabs) {
      const { container } = render(<Icon name={tab.icon!} />);
      expect(container.querySelector('svg')).not.toBeNull();
    }

    // [2026-06-01] Why: grep-based migration checks should not find old glyphs in
    // test source either. How: express legacy icon values with Unicode escapes instead
    // of literal symbols. Purpose: the repository can prove UI code has no leftover
    // emoji or Unicode icon glyphs while this regression assertion stays meaningful.
    const legacyIconNames = ['\u25cf', '\u2b21', '\u{1f4e1}'];
    expect(settingsTabs.map(tab => tab.icon)).not.toEqual(expect.arrayContaining(legacyIconNames));
  });
});
