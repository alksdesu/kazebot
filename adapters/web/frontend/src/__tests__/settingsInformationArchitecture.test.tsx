// 信息架构回归：哪一页归哪一组、混合保存有没有拆开、旧 id 与旧深链还能不能打开。
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { PersonaPage } from '../console/PersonaPage';
import { RuntimePage } from '../console/RuntimePage';
import { StickerCollectPage } from '../console/StickerCollectPage';
import { StickersPage } from '../console/StickersPage';
import { BOT_PAGES, MODEL_PAGES, QQ_SECTIONS } from '../console/pages';
import { resolveSettingsTabId, TAB_ALIASES } from '../components/settings/settingsTabAliases';
import { getSettingsTab, settingsTabs, type SettingsGroup } from '../components/settings/settingsTabs';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';

const idsInGroup = (group: SettingsGroup) =>
  settingsTabs.filter(tab => tab.group === group).map(tab => tab.id);

describe('设置分组归属', () => {
  it('机器人组放全平台共用的那几页', () => {
    expect(idsInGroup('bot')).toEqual([
      'persona', 'memory', 'stickers', 'community', 'conversation', 'reminders',
    ]);
  });

  it('渠道和模型不再登记在 QQ 页表里', () => {
    expect(idsInGroup('models')).toEqual(['providers', 'models']);
    expect(QQ_SECTIONS.map(section => section.id)).not.toContain('providers');
    expect(QQ_SECTIONS.map(section => section.id)).not.toContain('models');
    expect([...BOT_PAGES, ...MODEL_PAGES].map(page => page.id)).not.toContain('qq-account');
  });

  it('平台组只剩 QQ · NapCat 的分区', () => {
    expect(idsInGroup('platform')).toEqual([
      'qq-account', 'qq-channels', 'qq-trigger', 'qq-permissions',
      'qq-input', 'qq-output', 'qq-interaction', 'qq-sticker', 'qq-link',
    ]);
  });

  it('提醒从自动化里搬出来，自己一页', () => {
    expect(settingsTabs.find(tab => tab.id === 'reminders')).toMatchObject({ label: '提醒', group: 'bot' });
  });
});

describe('旧 id 别名', () => {
  it('每条别名都指向注册过的页，且自己不再占用 id', () => {
    const ids = new Set(settingsTabs.map(tab => tab.id));
    for (const [legacy, current] of Object.entries(TAB_ALIASES)) {
      expect(ids.has(current)).toBe(true);
      expect(ids.has(legacy)).toBe(false);
    }
  });

  it('没改名的 id 原样返回', () => {
    expect(resolveSettingsTabId('qq-channels')).toBe('qq-channels');
    expect(resolveSettingsTabId('general')).toBe('general');
  });

  it('getSettingsTab 认旧 id，不再落到「通用」', () => {
    expect(getSettingsTab('qq-persona').id).toBe('persona');
    expect(getSettingsTab('qq-models').id).toBe('models');
    expect(getSettingsTab('qq-timing').id).toBe('qq-trigger');
  });
});

describe('旧深链', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/');
    useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'general' });
  });

  afterEach(() => { window.history.replaceState(null, '', '/'); });

  it('切页签时把旧 id 换成新的，地址栏也跟着改写', () => {
    useViewStore.getState().setSettingsTab('qq-stickers');

    expect(useViewStore.getState().activeSettingsTab).toBe('stickers');
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('stickers');
  });

  it('openSettings 同样认旧 id', () => {
    useViewStore.getState().openSettings('qq-providers');

    expect(useViewStore.getState().activeSettingsTab).toBe('providers');
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('providers');
  });
});

describe('混合保存拆开', () => {
  beforeEach(() => {
    useSettingsStore.setState({ adminToken: '', isAuthenticated: false });
  });

  afterEach(cleanup);

  it('人格页只剩各自保存的人设与模型，发送处理搬去平台页', () => {
    render(<PersonaPage />);

    expect(screen.getByText('人设')).toBeInTheDocument();
    expect(screen.getByText('模型')).toBeInTheDocument();
    expect(screen.queryByText('回复格式')).toBeNull();
    expect(screen.queryByText('能接收什么')).toBeNull();
  });

  it('表情包图库只管图库，收集与发送在平台页', () => {
    render(<StickersPage />);

    expect(screen.getByText('图库')).toBeInTheDocument();
    expect(screen.queryByText('收集表情包')).toBeNull();

    cleanup();
    render(<StickerCollectPage />);
    expect(screen.getByText('收集表情包')).toBeInTheDocument();
  });

  it('链路页不再重复 supervisor 状态，那一行归引擎的运行页', () => {
    render(<RuntimePage />);

    expect(screen.getByText('消息队列')).toBeInTheDocument();
    expect(screen.queryByText('Supervisor')).toBeNull();
  });
});
