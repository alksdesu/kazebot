// 信息架构回归：哪一页归哪一组、混合保存有没有拆开、旧 id 与旧深链还能不能打开。
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { PersonaPage } from '../console/PersonaPage';
import { StickersPage } from '../console/StickersPage';
import { BOT_PAGES, MODEL_PAGES, PLATFORM_PAGES } from '../console/pages';
import { picksSection } from '../console/sectionPick';
import { resolveSettingsLocation, TAB_ALIASES } from '../components/settings/settingsTabAliases';
import { getSettingsTab, settingsTabs, type SettingsGroup } from '../components/settings/settingsTabs';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import { QQ_SCHEMA, QqSectionBody, snapshotFor } from './consoleSchemaFixture';

const idsInGroup = (group: SettingsGroup) =>
  settingsTabs.filter(tab => tab.group === group).map(tab => tab.id);

describe('设置分组归属', () => {
  it('机器人组放全平台共用的那几页', () => {
    expect(idsInGroup('bot')).toEqual([
      'persona', 'defaults', 'memory', 'stickers', 'community', 'conversation', 'reminders',
    ]);
  });

  it('渠道和模型不再登记在 QQ 页表里', () => {
    expect(idsInGroup('models')).toEqual(['providers', 'models']);
    const sectionIds = QQ_SCHEMA.sections.map(section => section.id);
    expect(sectionIds).not.toContain('providers');
    expect(sectionIds).not.toContain('models');
    expect([...BOT_PAGES, ...MODEL_PAGES].map(page => page.id)).not.toContain('account');
  });

  it('平台组一个平台一页，分区按 14.4 的清单折在页里', () => {
    expect(idsInGroup('platform')).toEqual(['platform-qq']);
    expect(QQ_SCHEMA.sections.map(section => section.id)).toEqual([
      'account', 'channels', 'trigger', 'permissions',
      'input', 'output', 'interaction', 'sticker', 'link',
    ]);
    expect(PLATFORM_PAGES[0].platform.id).toBe('qq');
  });

  it('提醒从自动化里搬出来，自己一页', () => {
    expect(settingsTabs.find(tab => tab.id === 'reminders')).toMatchObject({ label: '提醒', group: 'bot' });
  });
});

describe('旧 id 别名', () => {
  it('每条别名都指向注册过的页与分区，且自己不再占用 id', () => {
    const ids = new Set(settingsTabs.map(tab => tab.id));
    for (const [legacy, target] of Object.entries(TAB_ALIASES)) {
      expect(ids.has(target.tab)).toBe(true);
      expect(ids.has(legacy)).toBe(false);
      if (!target.section) continue;
      // 一份 schema 分摆几页时，别名只能指到这页真摆着的那块。
      const tab = settingsTabs.find(entry => entry.id === target.tab)!;
      const sections = tab.configScope ? snapshotFor(tab.configScope)?.sections || [] : [];
      const shown = sections.filter(section => picksSection(tab.sections, section.id));
      expect(shown.map(section => section.id)).toContain(target.section);
    }
  });

  it('没改名的 id 原样返回', () => {
    expect(resolveSettingsLocation('general')).toEqual({ tab: 'general', section: '' });
  });

  it('显式指定的分区压过别名自带的', () => {
    expect(resolveSettingsLocation('qq-timing')).toEqual({ tab: 'platform-qq', section: 'trigger' });
    expect(resolveSettingsLocation('qq-timing', 'link')).toEqual({ tab: 'platform-qq', section: 'link' });
  });

  it('getSettingsTab 认旧 id，不再落到「通用」', () => {
    expect(getSettingsTab('qq-persona').id).toBe('persona');
    expect(getSettingsTab('qq-models').id).toBe('models');
    expect(getSettingsTab('qq-timing').id).toBe('platform-qq');
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
    expect(new URLSearchParams(window.location.search).get('section')).toBeNull();
  });

  it('旧的分区页签落到平台页的对应分区上', () => {
    useViewStore.getState().setSettingsTab('qq-timing');

    expect(useViewStore.getState().activeSettingsTab).toBe('platform-qq');
    expect(useViewStore.getState().activeSettingsSection).toBe('trigger');
    const params = new URLSearchParams(window.location.search);
    expect(params.get('tab')).toBe('platform-qq');
    expect(params.get('section')).toBe('trigger');
  });

  it('换到别的页时不留下上一页的分区参数', () => {
    useViewStore.getState().setSettingsTab('qq-link');
    useViewStore.getState().setSettingsTab('memory');

    expect(useViewStore.getState().activeSettingsSection).toBe('');
    expect(new URLSearchParams(window.location.search).get('section')).toBeNull();
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
    render(<QqSectionBody id="sticker" />);
    expect(screen.getByText('收集表情包')).toBeInTheDocument();
  });

  it('链路页不再重复 supervisor 状态，那一行归引擎的运行页', () => {
    render(<QqSectionBody id="link" />);

    expect(screen.getByText('消息队列')).toBeInTheDocument();
    expect(screen.queryByText('Supervisor')).toBeNull();
  });
});
