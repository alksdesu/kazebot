// [2026-06-01] View registry and settings-tab tests for the new shell routing.
// Why: Settings is now a full application view instead of an App-level conditional
// branch. How: exercise viewStore transitions, the settings host fallback, and the
// settings sidebar tab list through public UI. Purpose: future settings pages can be
// registered without changing App.tsx or reintroducing modal/rightOverride logic.
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsPageHost } from '../components/settings/SettingsPageHost';
import { SettingsSidebar } from '../components/settings/SettingsSidebar';
import { currentSettingsTabs, resolveSettingsTab } from '../components/settings/settingsTabs';
import { adoptPlatforms, QQ_NAPCAT, resetPlatforms, type PlatformDescriptor } from '../platforms/registry';
import { useSettingsStore } from '../store/settingsStore';
import { viewRegistry, type AppViewContext } from '../views/viewRegistry';
import { useViewStore } from '../store/viewStore';

// viewRegistry 只该经这一个函数判要不要右栏。默认透传，个别用例才换掉它的答复。
vi.mock('../components/settings/settingsTabs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/settings/settingsTabs')>();
  return { ...actual, resolveSettingsTab: vi.fn(actual.resolveSettingsTab) };
});

const OTHERCHAT: PlatformDescriptor = {
  id: 'otherchat',
  displayName: 'OtherChat',
  shortName: 'Other',
  channels: [{ name: 'other_group', kind: 'group', hashed: true }],
  hasConfig: true,
  entrypoints: ['otherchat_bot.py'],
  connectionPeer: 'OtherChat 网关',
  connectionHint: '检查网关地址',
};

function responseJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('view registry settings flow', () => {
  beforeEach(() => {
    localStorage.clear();
    useViewStore.setState({ viewMode: 'chat', activeSettingsTab: 'general' });
    useSettingsStore.setState({
      adminToken: 'test-token',
      isAuthenticated: true,
      isConnected: true,
      entryNodeId: 'ereuna_main',
      availableNodes: [
        { id: 'ereuna_main', type: 'ai', name: 'EreunaMain', model: 'node-model' },
      ],
      modelConfig: { model: 'global-model', base_url: 'https://global.example/v1', api_key_present: true },
      activeNodeId: 'ereuna_main',
      activeNodeIsOverride: false,
      defaultNodeId: 'ereuna_main',
      globalModel: 'global-model',
      globalBaseUrl: 'https://global.example/v1',
      sessionProviderOverride: null,
      rightPanelOpen: true,
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/admin/auth/check')) return responseJson({ ok: true });
      if (url.endsWith('/admin/config/nodes')) return responseJson([{ id: 'ereuna_main', type: 'ai', name: 'EreunaMain' }]);
      if (url.endsWith('/config/openai/secret')) {
        return responseJson({ model: 'global-model', base_url: 'https://global.example/v1', api_key_present: true });
      }
      if (url.endsWith('/health')) return responseJson({ status: 'ok' });
      return responseJson({});
    }));
  });

  afterEach(() => {
    resetPlatforms();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('opens and closes settings through the dedicated view store', () => {
    useViewStore.getState().openSettings('tools');

    expect(useViewStore.getState().viewMode).toBe('settings');
    expect(useViewStore.getState().activeSettingsTab).toBe('tools');

    useViewStore.getState().closeSettings();

    expect(useViewStore.getState().viewMode).toBe('chat');
  });

  it('renders registered settings tabs and changes the active page without App conditionals', () => {
    render(<SettingsSidebar />);

    for (const tab of currentSettingsTabs()) {
      expect(screen.getByRole('button', { name: tab.label })).toBeInTheDocument();
    }
    expect(screen.getByRole('button', { name: '客户端' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '工具与权限' }));

    expect(useViewStore.getState().activeSettingsTab).toBe('tools');
  });

  it('falls back to the first registered settings page when the active tab is unknown', () => {
    useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'missing-tab' });

    render(<SettingsPageHost />);

    expect(screen.getByRole('heading', { name: '调度器连接' })).toBeInTheDocument();
  });

  it('右栏跟着解析结果开合，清单没到的平台页不先占一栏', () => {
    const hasRightPanel = (activeSettingsTab: string) =>
      viewRegistry.settings.hasRightPanel!({ activeSettingsTab } as AppViewContext);

    expect(hasRightPanel('tools')).toBe(true);
    expect(hasRightPanel('general')).toBe(false);
    // 清单还没到：认不出这一页不代表它没有，但也不能先按第一页的右栏布局。
    expect(resolveSettingsTab('platform-otherchat').pending).toBe(true);
    expect(hasRightPanel('platform-otherchat')).toBe(false);

    adoptPlatforms([QQ_NAPCAT, OTHERCHAT]);
    expect(hasRightPanel('platform-otherchat')).toBe(false);
    expect(hasRightPanel('tools')).toBe(true);
  });

  it('挂起的一页哪怕认出了带右栏的那个，也不留右栏', () => {
    const ask = () => viewRegistry.settings.hasRightPanel!({ activeSettingsTab: 'platform-otherchat' } as AppViewContext);
    const withPanel = currentSettingsTabs().find((tab) => tab.RightPanel)!;

    vi.mocked(resolveSettingsTab).mockReturnValueOnce({ tab: withPanel, pending: false });
    expect(ask()).toBe(true);

    vi.mocked(resolveSettingsTab).mockReturnValueOnce({ tab: withPanel, pending: true });
    expect(ask()).toBe(false);
  });

  it('keeps the settings right column dedicated to settings content', () => {
    // [2026-06-02] Regression coverage for the settings right rail. Why: settings mode
    // should no longer reserve the lower slot for EventLogPanel. How: assert the
    // registry leaves rightBottom empty while preserving the settings rightTop host.
    // Purpose: AppLayout can promote the settings panel to full height.
    expect(viewRegistry.settings.rightTop).toBeTypeOf('function');
    expect(viewRegistry.settings.rightBottom).toBeUndefined();
  });
});
