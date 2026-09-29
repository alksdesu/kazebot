// 侧栏搜索命中后，页那一行带不带分区定位。
import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { SettingsSidebar } from '../components/settings/SettingsSidebar';
import { STICKER_LIBRARY_SECTION } from '../console/FeatureSection';
import { platformScope } from '../console/configScope';
import { useSchemaStore } from '../console/schemaStore';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import { clearConsoleSchemas, QQ_SCHEMA, seedConsoleSchemas } from './consoleSchemaFixture';

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
  it('没有搜索时账号与连接就是机器人组里与人格同级的独立入口', () => {
    render(<SettingsSidebar />);
    const account = screen.getByRole('button', { name: '账号与连接' });
    const persona = screen.getByRole('button', { name: '人格' });
    const group = account.parentElement!.parentElement!;

    expect(persona.parentElement!.parentElement).toBe(group);
    expect(within(group).getByText('机器人')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '通用：账号与连接' })).not.toBeInTheDocument();
    fireEvent.click(account);
    expect(useViewStore.getState()).toMatchObject({ activeSettingsTab: 'account', activeSettingsSection: '' });
    expect(new URLSearchParams(window.location.search).has('section')).toBe(false);
  });

  it.each(['账号与连接', '多开实例', '共享配置', '机器人账号', '快速切换', '记忆归属'])('没有 schema 也能从 %s 找到独立账号页', (text) => {
    clearConsoleSchemas();
    render(<SettingsSidebar />);

    search(text);
    fireEvent.click(screen.getByRole('button', { name: '账号与连接' }));
    expect(useViewStore.getState()).toMatchObject({ activeSettingsTab: 'account', activeSettingsSection: '' });
    const params = new URLSearchParams(window.location.search);
    expect(params.get('tab')).toBe('account');
    expect(params.has('section')).toBe(false);
  });

  it('旧后端 QQ schema 里的账号分区不再产生平台搜索入口', () => {
    useSchemaStore.setState((state) => ({ schemas: {
      ...state.schemas,
      [platformScope('qq').id]: {
        ...QQ_SCHEMA,
        sections: [{ id: 'account', label: '账号与连接', icon: 'account_circle', note: '', paths: [], nodes: [] },
          ...QQ_SCHEMA.sections.filter(section => section.id !== 'account')],
      },
    } }));
    render(<SettingsSidebar />);

    search('账号与连接');
    expect(screen.getByRole('button', { name: '账号与连接' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '通用：账号与连接' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'QQ · NapCat：账号与连接' })).not.toBeInTheDocument();
  });

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
