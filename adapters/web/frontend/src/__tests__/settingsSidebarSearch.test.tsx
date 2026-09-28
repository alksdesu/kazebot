// 侧栏搜索命中后，页那一行带不带分区定位。
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { SettingsSidebar } from '../components/settings/SettingsSidebar';
import { STICKER_LIBRARY_SECTION } from '../console/FeatureSection';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import { clearConsoleSchemas, seedConsoleSchemas } from './consoleSchemaFixture';

const search = (text: string) => fireEvent.change(screen.getByLabelText('搜索设置'), { target: { value: text } });

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, '', '/');
  // 空令牌让侧栏跳过拉取，分区全部来自灌进去的那份快照。
  useSettingsStore.setState({ adminToken: '', isAuthenticated: true, isConnected: true });
  useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'general', activeSettingsSection: '' });
  seedConsoleSchemas();
});

afterEach(() => {
  clearConsoleSchemas();
});

describe('侧栏搜索命中的页那一行', () => {
  it('并进来的分区跳到那一块，没并进来的把上一次的定位清掉', () => {
    render(<SettingsSidebar />);

    search('跳过人工筛选');
    fireEvent.click(screen.getByRole('button', { name: '表情包图库' }));
    expect(useViewStore.getState().activeSettingsTab).toBe('stickers');
    expect(useViewStore.getState().activeSettingsSection).toBe(STICKER_LIBRARY_SECTION);

    search('跟复读');
    fireEvent.click(screen.getByRole('button', { name: '全局默认' }));
    expect(useViewStore.getState().activeSettingsTab).toBe('defaults');
    expect(useViewStore.getState().activeSettingsSection).toBe('');
  });
});
