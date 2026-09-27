// 平台页的折叠分区：摘要只看生效值、待应用按配置段归位、深链能展开、展开状态记得住。
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConfigLiveState } from '../api/supervisorClient';
import { getSettingsTab } from '../components/settings/settingsTabs';
import { platformScope } from '../console/configScope';
import { platformConsoleStore } from '../console/consoleStore';
import { useSchemaStore } from '../console/schemaStore';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import { clearConsoleSchemas, seedConsoleSchemas } from './consoleSchemaFixture';

const SECTION_STORE_KEY = 'console_sections_qq';

const live = (values: Record<string, unknown> = {}): ConfigLiveState => ({
  published: true,
  applied: true,
  file: { exists: true, mtime_ns: 1, size: 1 },
  state: {
    values: {
      allowed_groups: [101, 102],
      allow_private_friends: true,
      signal_at: true,
      signal_reply: true,
      cooldown_group_sec: 30,
      enable_queue: true,
      queue_workers: 2,
      enable_forward_bridge: true,
      ...values,
    },
    paths: {
      allowed_groups: 'channels.allowed_groups',
      allow_private_friends: 'channels.allow_private_friends',
      signal_at: 'trigger.signals.at',
      signal_reply: 'trigger.signals.reply',
      cooldown_group_sec: 'trigger.cooldown.per_group_sec',
      enable_queue: 'queue.enabled',
      queue_workers: 'queue.workers',
      enable_forward_bridge: 'forward_bridge.enabled',
      enable_reactions: 'extensions.reactions',
      enable_echo: 'extensions.echo.enabled',
      auto_like_times: 'extensions.auto_like_times',
      sticker_collect: 'extensions.sticker.collect',
      grant_restart: 'permissions.capabilities.restart',
      approval_bare_verb_unique: 'approval.bare_verb_unique',
      enable_image_input: 'input.image',
      message_limit: 'output.message_limit',
    },
  },
});

const PlatformTab = () => {
  const Page = getSettingsTab('platform-qq').Page;
  return <Page />;
};

const header = (name: string) => screen.getByRole('button', { name: new RegExp(`^${name}`) });

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, '', '/');
  useSettingsStore.setState({ adminToken: '', isAuthenticated: true });
  useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'platform-qq', activeSettingsSection: '' });
  platformConsoleStore('qq').setState({ live: live(), draft: {}, raw: '', loading: false, saving: false, error: '', notice: '' });
  seedConsoleSchemas();
  // jsdom 没有 confirm，不打桩的话「丢弃」的二次确认永远拿到 undefined。
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  clearConsoleSchemas();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('折叠分区', () => {
  it('收起时给一行摘要，读的是生效值不是草稿', () => {
    platformConsoleStore('qq').setState({ draft: { allowed_groups: [101, 102, 103, 104] } });
    render(<PlatformTab />);

    expect(header('信道')).toHaveTextContent('2 个群');
    expect(header('触发时机')).toHaveTextContent('冷却 30 秒');
    expect(header('链路')).toHaveTextContent('队列 2 个 worker');
  });

  it('折叠头是按钮，展开状态报给读屏', () => {
    render(<PlatformTab />);
    const button = header('信道');

    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(button).toHaveAttribute('aria-controls');

    fireEvent.click(button);
    expect(header('信道')).toHaveAttribute('aria-expanded', 'true');
  });

  it('展开之后才渲染正文，收起就不占位', () => {
    render(<PlatformTab />);
    expect(screen.queryByText('允许说话的群')).toBeNull();

    fireEvent.click(header('信道'));
    expect(screen.getByText('允许说话的群')).toBeInTheDocument();
  });

  it('展开状态记在这台浏览器上', () => {
    const view = render(<PlatformTab />);
    fireEvent.click(header('触发时机'));
    expect(JSON.parse(localStorage.getItem(SECTION_STORE_KEY)!)).toEqual({ trigger: true });

    view.unmount();
    render(<PlatformTab />);
    expect(header('触发时机')).toHaveAttribute('aria-expanded', 'true');
  });

  it('localStorage 读不了也照样能用', () => {
    localStorage.setItem(SECTION_STORE_KEY, '{not json');
    render(<PlatformTab />);

    expect(header('信道')).toHaveAttribute('aria-expanded', 'false');
  });
});

describe('待应用归位', () => {
  it('按 bot 公布的配置段算在对应分区头上，总数仍由状态栏给', () => {
    platformConsoleStore('qq').setState({
      draft: { allowed_groups: [101], signal_at: false, cooldown_group_sec: 60, enable_queue: false },
    });
    render(<PlatformTab />);

    expect(header('信道')).toHaveTextContent('1 项待应用');
    expect(header('触发时机')).toHaveTextContent('2 项待应用');
    expect(header('链路')).toHaveTextContent('1 项待应用');
    expect(screen.getByText('4 项待应用')).toBeInTheDocument();
  });

  it('同级键不会被邻居的前缀吃掉', () => {
    platformConsoleStore('qq').setState({ draft: { enable_reactions: false, auto_like_times: 3 } });
    render(<PlatformTab />);

    expect(header('输出格式')).toHaveTextContent('1 项待应用');
    expect(header('群互动')).toHaveTextContent('1 项待应用');
  });

  it('权限段和审批段都算权限分区的', () => {
    platformConsoleStore('qq').setState({ draft: { grant_restart: 'admin', approval_bare_verb_unique: false } });
    render(<PlatformTab />);

    expect(header('权限')).toHaveTextContent('2 项待应用');
  });

  it('认不出配置段的改动不算进任何分区，但状态栏不会漏掉它', () => {
    platformConsoleStore('qq').setState({ draft: { brand_new_key: 1 } });
    render(<PlatformTab />);

    expect(header('信道')).not.toHaveTextContent('待应用');
    expect(screen.getByText('1 项待应用')).toBeInTheDocument();
  });
});

describe('分区深链', () => {
  it('?section= 指名的那一块无条件展开，记住的收起状态挡不住', () => {
    localStorage.setItem(SECTION_STORE_KEY, JSON.stringify({ trigger: false }));
    useViewStore.setState({ activeSettingsSection: 'trigger' });
    render(<PlatformTab />);

    expect(header('触发时机')).toHaveAttribute('aria-expanded', 'true');
    expect(header('信道')).toHaveAttribute('aria-expanded', 'false');
  });

  it('bot 没上报时其余分区不出现，账号与连接照样能用', () => {
    platformConsoleStore('qq').setState({ live: { published: false, file: { exists: false, mtime_ns: 0, size: 0 } } });
    render(<PlatformTab />);

    expect(screen.getByRole('button', { name: /^账号与连接/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^信道/ })).toBeNull();
  });
});

describe('控制台布局', () => {
  it('布局还在路上时说一声，不拿空白冒充没有分区', () => {
    clearConsoleSchemas();
    useSettingsStore.setState({ adminToken: 'fixture-token' });
    // 请求永远不回，页面停在读取中。
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined)));
    render(<PlatformTab />);

    expect(screen.getByText('正在读取控制台布局…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^信道/ })).toBeNull();
  });

  it('布局拉不到时把原因摆出来', () => {
    useSchemaStore.setState({
      schemas: {},
      errors: { [platformScope('qq').id]: '读不到控制台布局（404 Not Found）。supervisor 可能还没更新到当前版本，重启 supervisor 之后再试。' },
    });
    render(<PlatformTab />);

    expect(screen.getByText(/重启 supervisor 之后再试/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^信道/ })).toBeNull();
  });
});

describe('顶部状态栏', () => {
  it('没有待应用改动时应用和丢弃都点不动', () => {
    render(<PlatformTab />);
    const bar = screen.getByText('已生效').parentElement!;

    expect(within(bar).getByRole('button', { name: '应用' })).toBeDisabled();
    expect(within(bar).getByRole('button', { name: '丢弃' })).toBeDisabled();
  });

  it('丢弃清掉全部草稿，各分区的计数一起归零', () => {
    platformConsoleStore('qq').setState({ draft: { allowed_groups: [101] } });
    render(<PlatformTab />);

    fireEvent.click(screen.getByRole('button', { name: '丢弃' }));

    expect(platformConsoleStore('qq').getState().draft).toEqual({});
    expect(header('信道')).not.toHaveTextContent('待应用');
  });
});
