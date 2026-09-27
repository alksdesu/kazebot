// 「平台」组跟着后端清单走：后端新出一个平台，侧栏就多一页，分区全按它自己的 schema 画。
// 清单还在路上时深链到新平台页不能被冲回「通用」，清单到了之后同一个平台不能换组件重挂载。
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConsoleSchema, PlatformSummary } from '../api/supervisorClient';
import { SettingsHeader } from '../components/settings/SettingsHeader';
import { SettingsPageHost } from '../components/settings/SettingsPageHost';
import { SettingsSidebar } from '../components/settings/SettingsSidebar';
import {
  currentSettingsTabs,
  resolveSettingsTab,
  settingsTabsFor,
  useResolvedSettingsTab,
  type ResolvedSettingsTab,
} from '../components/settings/settingsTabs';
import { platformScope } from '../console/configScope';
import { platformConsoleStore } from '../console/consoleStore';
import { DEFAULT_PLATFORM_ICON, platformPageComponent, platformPages } from '../console/pages';
import { useSchemaStore } from '../console/schemaStore';
import {
  adoptPlatforms,
  allPlatforms,
  platformRegistryState,
  QQ_NAPCAT,
  resetPlatforms,
  settlePlatforms,
  type PlatformDescriptor,
} from '../platforms/registry';
import { usePlatformRegistry } from '../platforms/usePlatformRegistry';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import { clearConsoleSchemas, seedConsoleSchemas } from './consoleSchemaFixture';

const OTHERCHAT: PlatformDescriptor = {
  id: 'otherchat',
  displayName: 'OtherChat',
  shortName: 'Other',
  channels: [{ name: 'other_group', kind: 'group', hashed: true }],
  hasConfig: true,
  entrypoints: ['otherchat_bot.py'],
  connectionPeer: 'OtherChat 网关',
  connectionHint: '检查网关地址与接入密钥',
};

// 没有配置文件的平台（比如只发不收的通知渠道）：认会话键要它，但没什么可设置的。
const NOTIFY: PlatformDescriptor = {
  id: 'notify',
  displayName: 'Notify',
  shortName: 'Notify',
  channels: [{ name: 'notify_push', kind: 'other', hashed: false }],
  hasConfig: false,
  entrypoints: [],
  connectionPeer: '',
  connectionHint: '',
};

// account 这个分区 id 在 QQ 页上挂着前端画的摘要，这里用同一个 id 验证它不会漏到别的平台。
const OTHER_SCHEMA: ConsoleSchema = {
  version: 1,
  sections: [
    { id: 'account', label: '账号', icon: 'cable', note: '这个号是谁。', paths: [], nodes: [] },
    { id: 'channels', label: '信道', icon: 'forum', note: '放行哪些会话。', paths: ['channels'], nodes: [] },
  ],
  fields: {},
};

const OTHER_ROW: PlatformSummary = {
  id: 'otherchat',
  display_name: 'OtherChat',
  short_name: 'Other',
  id_shape: 'string',
  channels: [{ name: 'other_group', kind: 'group', hashed: true }],
  has_config: true,
  hooks: [],
  entrypoints: ['otherchat_bot.py'],
  connection_peer: 'OtherChat 网关',
  connection_hint: '检查网关地址与接入密钥',
};

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });

/** 后端只认得 otherchat 这一个新平台；记下每个请求的路径，供断言拉没拉 schema。 */
function stubBackend(): string[] {
  const seen: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    seen.push(url);
    if (url.endsWith('/config/schema')) return json(OTHER_SCHEMA);
    if (url.endsWith('/config/state')) return json({ published: false, applied: false, file: {}, state: null });
    if (url.endsWith('/config/raw')) return json({ content: '', exists: false });
    return json({});
  }));
  return seen;
}

const pathsOf = (seen: readonly string[], leaf: string) => seen.filter((url) => url.endsWith(leaf));

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, '', '/');
  resetPlatforms();
  clearConsoleSchemas();
  useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'general', activeSettingsSection: '' });
  useSettingsStore.setState({ adminToken: '', isAuthenticated: true, isConnected: true });
  platformConsoleStore('otherchat').setState({ live: null, draft: {}, raw: '', loading: false, saving: false, error: '', notice: '' });
});

afterEach(() => {
  cleanup();
  resetPlatforms();
  clearConsoleSchemas();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('平台页跟着清单生成', () => {
  it('只有带配置文件的平台才有页，图标缺省是 inbox', () => {
    const pages = platformPages([QQ_NAPCAT, OTHERCHAT, NOTIFY]);

    expect(pages.map((page) => page.platformId)).toEqual(['qq', 'otherchat']);
    expect(pages[1]).toMatchObject({ id: 'platform-otherchat', label: 'OtherChat', icon: DEFAULT_PLATFORM_ICON });
  });

  it('说明文字按平台的简称写，QQ 那句一个字没变', () => {
    const [qq, other] = platformPages([QQ_NAPCAT, OTHERCHAT]);

    expect(qq.note).toBe('bot 在 QQ 上怎么收发消息。改完点顶部的「应用」写进配置文件，状态条会显示 bot 有没有读到这一份。');
    expect(other.note).toBe('bot 在 Other 上怎么收发消息。改完点顶部的「应用」写进配置文件，状态条会显示 bot 有没有读到这一份。');
  });

  it('同一个平台永远是同一个组件 —— 换引用就会把整页重新挂载', () => {
    const first = platformPageComponent('otherchat');

    expect(platformPageComponent('otherchat')).toBe(first);
    expect(platformPageComponent('qq')).not.toBe(first);
  });

  it('清单没换就还是同一张页签表，换了才重排', () => {
    const platforms = [QQ_NAPCAT, OTHERCHAT];

    expect(settingsTabsFor(platforms)).toBe(settingsTabsFor(platforms));
    expect(settingsTabsFor([QQ_NAPCAT, OTHERCHAT])).not.toBe(settingsTabsFor(platforms));
  });

  it('平台页签排在固定页签后面，走自己那份配置作用域', () => {
    adoptPlatforms([QQ_NAPCAT, OTHERCHAT]);
    const tab = currentSettingsTabs().find((entry) => entry.id === 'platform-otherchat')!;

    expect(tab).toMatchObject({ group: 'platform', order: 41, label: 'OtherChat' });
    expect(tab.configScope).toBe(platformScope('otherchat'));
    expect(tab.Page).toBe(platformPageComponent('otherchat'));
  });
});

describe('从后端拉清单', () => {
  const Probe = () => { usePlatformRegistry(); return null; };

  it('拉到了就换上这一份，不用再等', async () => {
    useSettingsStore.setState({ adminToken: 'admin-token' });
    vi.stubGlobal('fetch', vi.fn(async () => json({ platforms: [OTHER_ROW] })));

    render(<Probe />);

    await waitFor(() => expect(platformRegistryState().settled).toBe(true));
    expect(allPlatforms().map((platform) => platform.id)).toEqual(['otherchat']);
  });

  it('拉不到也得停下来 —— 不然深链到的平台页会一直说在读清单', async () => {
    useSettingsStore.setState({ adminToken: 'admin-token' });
    vi.stubGlobal('fetch', vi.fn(async () => new Response('nope', { status: 500 })));

    render(<Probe />);

    await waitFor(() => expect(platformRegistryState().settled).toBe(true));
    expect(allPlatforms()).toEqual([QQ_NAPCAT]);
  });

  it('没登录就不拉，也不假装清单已经到了', async () => {
    const fetchMock = vi.fn(async () => json({ platforms: [OTHER_ROW] }));
    vi.stubGlobal('fetch', fetchMock);

    render(<Probe />);
    await act(async () => { await Promise.resolve(); });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(platformRegistryState().settled).toBe(false);
  });
});

// 组件里外两种解析必须同一个判法：布局按组件外那次决定留不留右栏，页面按 hook 那次决定画什么。
describe('页签解析', () => {
  const seenByHook = (tabId: string): ResolvedSettingsTab => {
    let seen: ResolvedSettingsTab | null = null;
    const Probe = () => { seen = useResolvedSettingsTab(tabId); return null; };
    render(<Probe />);
    cleanup();
    return seen!;
  };

  const sameAsHook = (tabId: string) => {
    const outside = resolveSettingsTab(tabId);
    const inside = seenByHook(tabId);
    expect(outside.pending).toBe(inside.pending);
    expect(outside.tab.id).toBe(inside.tab.id);
    expect(outside.tab.Page).toBe(inside.tab.Page);
  };

  it('组件外和 hook 对上：认得的页、旧 id、还没进清单的平台页', () => {
    sameAsHook('tools');
    sameAsHook('qq-timing');
    sameAsHook('platform-otherchat');

    adoptPlatforms([QQ_NAPCAT, OTHERCHAT]);
    sameAsHook('platform-otherchat');
    sameAsHook('platform-gone');
  });

  it('清单还没到的平台页先挂起，不算解析失败', () => {
    expect(resolveSettingsTab('platform-otherchat')).toMatchObject({ pending: true, tab: { id: 'general' } });
    expect(resolveSettingsTab('platform-qq')).toMatchObject({ pending: false, tab: { id: 'platform-qq' } });
    expect(resolveSettingsTab('no-such-page')).toMatchObject({ pending: false, tab: { id: 'general' } });
  });

  it('给了清单就按给的判，不看眼下这一份', () => {
    const arrived = resolveSettingsTab('platform-otherchat', { platforms: [QQ_NAPCAT, OTHERCHAT], settled: true });
    expect(arrived).toMatchObject({ pending: false, tab: { id: 'platform-otherchat' } });

    adoptPlatforms([QQ_NAPCAT, OTHERCHAT]);
    const waiting = resolveSettingsTab('platform-otherchat', { platforms: [QQ_NAPCAT], settled: false });
    expect(waiting).toMatchObject({ pending: true, tab: { id: 'general' } });
    // 清单到了也没有这个平台，才是真的解析不出来。
    expect(resolveSettingsTab('platform-gone', { platforms: [QQ_NAPCAT], settled: true }))
      .toMatchObject({ pending: false, tab: { id: 'general' } });
  });
});

describe('侧栏的「平台」组', () => {
  it('清单一到，新平台就出现在组里，点一下就进去', () => {
    seedConsoleSchemas();
    render(<SettingsSidebar />);
    expect(screen.queryByRole('button', { name: 'OtherChat' })).toBeNull();

    act(() => { adoptPlatforms([QQ_NAPCAT, OTHERCHAT]); });

    fireEvent.click(screen.getByRole('button', { name: 'OtherChat' }));
    expect(useViewStore.getState().activeSettingsTab).toBe('platform-otherchat');
    expect(screen.getByRole('button', { name: 'QQ · NapCat' })).toBeInTheDocument();
  });

  it('没有配置文件的平台不占一页', () => {
    seedConsoleSchemas();
    render(<SettingsSidebar />);

    act(() => { adoptPlatforms([QQ_NAPCAT, NOTIFY]); });

    expect(screen.queryByRole('button', { name: 'Notify' })).toBeNull();
    expect(screen.getByText('平台')).toBeInTheDocument();
  });

  it('一个带配置的平台都没有时，整个「平台」组不出现', () => {
    seedConsoleSchemas();
    render(<SettingsSidebar />);

    act(() => { adoptPlatforms([NOTIFY]); });

    expect(screen.queryByText('平台')).toBeNull();
    expect(currentSettingsTabs().filter((tab) => tab.group === 'platform')).toEqual([]);
  });

  it('新平台进清单之后，它的分区布局也跟着拉下来，一登录就搜得到', async () => {
    useSettingsStore.setState({ adminToken: 'admin-token' });
    const seen = stubBackend();
    render(<SettingsSidebar />);

    act(() => { adoptPlatforms([QQ_NAPCAT, OTHERCHAT]); });

    await waitFor(() => expect(useSchemaStore.getState().schemas[platformScope('otherchat').id]).toBeTruthy());
    expect(pathsOf(seen, '/v1/platforms/otherchat/config/schema')).toHaveLength(1);

    fireEvent.change(screen.getByLabelText('搜索设置'), { target: { value: '信道' } });
    expect(screen.getByRole('button', { name: 'OtherChat：信道' })).toBeInTheDocument();
  });
});

describe('没登记过的平台页', () => {
  const OtherPage = () => {
    const { tab } = useResolvedSettingsTab('platform-otherchat');
    return <tab.Page />;
  };

  it('分区全按后端 schema 画，不蹭 QQ 页上前端自己那几块', async () => {
    useSettingsStore.setState({ adminToken: 'admin-token' });
    const seen = stubBackend();
    adoptPlatforms([QQ_NAPCAT, OTHERCHAT]);

    render(<OtherPage />);

    const account = await screen.findByRole('button', { name: /^账号/ });
    expect(screen.getByRole('heading', { name: 'OtherChat' })).toBeInTheDocument();
    // QQ 的 account 摘要挂在同名分区 id 上，漏过来的话这个折叠头会多出一行进程状态。
    expect(account).toHaveTextContent(/^账号$/);
    expect(screen.queryByText('适配进程没在跑')).toBeNull();
    // 写配置的分区在 bot 没上报时不画。
    expect(screen.queryByRole('button', { name: /^信道/ })).toBeNull();
    expect(pathsOf(seen, '/v1/platforms/otherchat/config/schema')).toHaveLength(1);
  });

  it('清单换一份新数组时这一页不重新挂载：本地勾选还在，配置也不再拉一遍', async () => {
    useSettingsStore.setState({ adminToken: 'admin-token' });
    const seen = stubBackend();
    adoptPlatforms([QQ_NAPCAT, OTHERCHAT]);
    render(<OtherPage />);
    const heading = await screen.findByRole('heading', { name: 'OtherChat' });
    const before = pathsOf(seen, '/v1/platforms/otherchat/config/state').length;
    // 只存在这一次挂载里的状态：重新挂载就会掉回没勾。
    fireEvent.click(screen.getByLabelText('附带脱敏日志'));

    act(() => { adoptPlatforms([{ ...QQ_NAPCAT }, { ...OTHERCHAT }]); });

    expect(screen.getByRole('heading', { name: 'OtherChat' })).toBe(heading);
    expect(screen.getByLabelText('附带脱敏日志')).toBeChecked();
    expect(pathsOf(seen, '/v1/platforms/otherchat/config/state')).toHaveLength(before);
  });

  it('平台从清单里消失之后，这一页说清楚是怎么回事', () => {
    adoptPlatforms([QQ_NAPCAT, OTHERCHAT]);
    const Page = platformPageComponent('otherchat');
    render(<Page />);

    act(() => { adoptPlatforms([QQ_NAPCAT]); });

    expect(screen.getByRole('heading', { name: 'otherchat' })).toBeInTheDocument();
    expect(screen.getByText(/平台清单里已经没有「otherchat」了/)).toBeInTheDocument();
  });
});

describe('bot 还没上报时的提示', () => {
  const pageFor = (platform: PlatformDescriptor) => {
    adoptPlatforms([QQ_NAPCAT, platform].filter((row, index, rows) => rows.indexOf(row) === index));
    const Page = platformPageComponent(platform.id);
    return render(<Page />);
  };

  it('QQ 那句一个字没变', () => {
    platformConsoleStore('qq').setState({ live: null, loading: false, draft: {} });
    pageFor(QQ_NAPCAT);

    expect(screen.getByText(
      'bot 还没上报生效配置。在服务器上启动 QQ · NapCat 的适配器进程（bot.py）之后，这里会显示它实际在用的值。',
    )).toBeInTheDocument();
  });

  it('新平台按它自己声明的入口脚本说', () => {
    pageFor(OTHERCHAT);

    expect(screen.getByText(
      'bot 还没上报生效配置。在服务器上启动 OtherChat 的适配器进程（otherchat_bot.py）之后，这里会显示它实际在用的值。',
    )).toBeInTheDocument();
  });

  it('没声明入口的平台不留一个空括号', () => {
    pageFor({ ...OTHERCHAT, entrypoints: [] });

    expect(screen.getByText(
      'bot 还没上报生效配置。在服务器上启动 OtherChat 的适配器进程之后，这里会显示它实际在用的值。',
    )).toBeInTheDocument();
  });
});

describe('深链到清单还没到的平台页', () => {
  beforeEach(() => {
    useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'platform-otherchat' });
  });

  it('先说在读清单，不把地址栏冲回「通用」', () => {
    render(<><SettingsHeader /><SettingsPageHost /></>);

    expect(screen.getAllByText('正在读取平台清单…')).toHaveLength(2);
    expect(useViewStore.getState().activeSettingsTab).toBe('platform-otherchat');
  });

  it('清单到了就画这一页', async () => {
    useSettingsStore.setState({ adminToken: 'admin-token' });
    stubBackend();
    render(<><SettingsHeader /><SettingsPageHost /></>);

    act(() => { adoptPlatforms([QQ_NAPCAT, OTHERCHAT]); });

    // 标题栏一个、页面本身一个。
    expect(await screen.findAllByRole('heading', { name: 'OtherChat' })).toHaveLength(2);
    expect(screen.queryByText('正在读取平台清单…')).toBeNull();
    expect(useViewStore.getState().activeSettingsTab).toBe('platform-otherchat');
  });

  it('清单到了也没有这个平台，才退回第一页', () => {
    render(<SettingsHeader />);

    act(() => { settlePlatforms(); });

    expect(useViewStore.getState().activeSettingsTab).toBe('general');
  });

  it('内置就有的平台页不用等清单', () => {
    useViewStore.setState({ activeSettingsTab: 'platform-qq' });
    platformConsoleStore('qq').setState({ live: null, loading: false, draft: {} });

    render(<SettingsHeader />);

    expect(screen.queryByText('正在读取平台清单…')).toBeNull();
    expect(screen.getByText('QQ · NapCat')).toBeInTheDocument();
    expect(useViewStore.getState().activeSettingsTab).toBe('platform-qq');
  });

  it('清单没到时侧栏一项都不高亮，不先把「通用」点亮', () => {
    seedConsoleSchemas();
    render(<SettingsSidebar />);
    expect(document.querySelector('[aria-current="page"]')).toBeNull();

    act(() => { adoptPlatforms([QQ_NAPCAT, OTHERCHAT]); });

    expect(screen.getByRole('button', { name: 'OtherChat' })).toHaveAttribute('aria-current', 'page');
  });

  it('清单没到但要的不是平台页时照旧退回第一页', () => {
    useViewStore.setState({ activeSettingsTab: 'no-such-page' });

    render(<SettingsHeader />);

    expect(allPlatforms()).toEqual([QQ_NAPCAT]);
    expect(useViewStore.getState().activeSettingsTab).toBe('general');
  });
});
