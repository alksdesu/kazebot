// 平台页的折叠分区：摘要只看生效值、待应用按配置段归位、深链能展开、展开状态记得住。
// 页头那几条交代（状态条、警告、读取与解析失败）和跟着实况刷新也在这里。
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConfigLiveState } from '../api/supervisorClient';
import { getSettingsTab } from '../components/settings/settingsTabs';
import { platformScope } from '../console/configScope';
import { platformConsoleStore } from '../console/consoleStore';
import { useSchemaStore } from '../console/schemaStore';
import { Section } from '../console/Section';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import { clearConsoleSchemas, QQ_SCHEMA, seedConsoleSchemas } from './consoleSchemaFixture';

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

const NOT_PUBLISHED = 'bot 还没上报生效配置。在服务器上启动 QQ · NapCat 的适配器进程（bot.py）之后，这里会显示它实际在用的值。';
const UNCONFIRMED = '已应用，但 bot 没在 60 秒内确认生效。它可能没在跑，去服务器上看这个平台的适配器进程（bot.py）。';

const PlatformTab = () => {
  const Page = getSettingsTab('platform-qq').Page;
  return <Page />;
};

const header = (name: string) => screen.getByRole('button', { name: new RegExp(`^${name}`) });

const sectionBody = (name: string) => document.getElementById(header(name).getAttribute('aria-controls')!)!;

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, '', '/');
  useSettingsStore.setState({ adminToken: '', isAuthenticated: true });
  useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'platform-qq', activeSettingsSection: '' });
  platformConsoleStore('qq').setState({ live: live(), draft: {}, raw: '', loading: false, saving: false, loadError: '', applyError: '', notice: '', confirmation: null });
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
    expect(screen.queryByRole('button', { name: /^账号与连接/ })).toBeNull();
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

  it('收起时正文容器还留着，只是藏起来 —— aria-controls 指的元素不能不在', () => {
    render(<PlatformTab />);
    const body = sectionBody('信道');

    expect(body).toBeInTheDocument();
    expect(body).not.toBeVisible();
    expect(body).toBeEmptyDOMElement();

    fireEvent.click(header('信道'));

    expect(sectionBody('信道')).toBeVisible();
    expect(within(sectionBody('信道')).getByText('允许说话的群')).toBeInTheDocument();
  });

  it('按钮的名字只念标题和要紧的标记，摘要另外作为描述念', () => {
    platformConsoleStore('qq').setState({ draft: { allowed_groups: [101] } });
    render(<PlatformTab />);

    expect(header('信道')).toHaveAccessibleName('信道 1 项待应用');
    expect(header('信道')).toHaveAccessibleDescription('2 个群 · 私聊仅好友');
    expect(header('触发时机')).toHaveAccessibleName('触发时机');
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

// 眼下没有页面给分区传 error，名字的拼法还是得连这一档一起定死。
describe('分区头的可访问名称', () => {
  const renderSection = (props: Partial<ComponentProps<typeof Section>>) => render(
    <Section id="demo" onToggle={() => undefined} open={false} summary="一行摘要" title="示例" {...props}>
      <p>正文</p>
    </Section>,
  );

  it('标题之外只念「配置有误」和待办计数，摘要另作描述', () => {
    renderSection({ error: '第 3 行缩进不对', pending: 2 });

    expect(screen.getByRole('button')).toHaveAccessibleName('示例 配置有误 2 项待应用');
    expect(screen.getByRole('button')).toHaveAccessibleDescription('一行摘要');
  });

  it('没有摘要就不留一个空描述', () => {
    renderSection({ summary: null });

    expect(screen.getByRole('button')).toHaveAccessibleName('示例');
    expect(screen.getByRole('button')).not.toHaveAttribute('aria-describedby');
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

  it('bot 没上报时配置分区不出现，平台页不再提供账号与连接', () => {
    platformConsoleStore('qq').setState({ live: { published: false, file: { exists: false, mtime_ns: 0, size: 0 } } });
    render(<PlatformTab />);

    expect(screen.queryByRole('button', { name: /^账号与连接/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^信道/ })).toBeNull();
  });
});

describe('控制台布局', () => {
  it.each([true, false])('旧后端的 QQ 账号分区不会重新出现（已上报：%s）', (published) => {
    platformConsoleStore('qq').setState({ live: { ...live(), published } });
    useSchemaStore.setState({
      schemas: {
        ...useSchemaStore.getState().schemas,
        [platformScope('qq').id]: {
          ...QQ_SCHEMA,
          sections: [
            { id: 'account', label: '账号与连接', icon: 'account_circle', note: '', paths: [], nodes: [] },
            ...QQ_SCHEMA.sections,
          ],
        },
      },
    });
    useViewStore.setState({ activeSettingsSection: 'account' });
    render(<PlatformTab />);

    expect(screen.queryByRole('button', { name: /^账号与连接/ })).toBeNull();
    expect(Boolean(screen.queryByRole('button', { name: /^信道/ }))).toBe(published);
  });

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
    const bar = screen.getByText('已生效').closest('div')!;

    expect(within(bar).getByRole('button', { name: '应用' })).toBeDisabled();
    expect(within(bar).getByRole('button', { name: '丢弃' })).toBeDisabled();
  });

  it('写完还没等到 bot 认账时说在等，不报成出了错', () => {
    platformConsoleStore('qq').setState({ live: { ...live(), applied: false }, confirmation: 'waiting' });
    render(<PlatformTab />);

    expect(screen.getByText('已写入，等 bot 确认生效…')).toBeInTheDocument();
    expect(screen.queryByText('bot 用的还是旧配置')).toBeNull();
  });

  it('等认账期间语法错误和心跳中断照样压在上面', () => {
    const broken = live();
    platformConsoleStore('qq').setState({
      live: { ...broken, applied: false, state: { ...broken.state!, parse_error: 'line 3: bad indent' } },
      confirmation: 'waiting',
    });
    const view = render(<PlatformTab />);
    expect(screen.getByText('配置有语法错误，bot 仍在用上一份')).toBeInTheDocument();

    view.unmount();
    platformConsoleStore('qq').setState({ live: { ...live(), applied: false, stale: true }, confirmation: 'waiting' });
    render(<PlatformTab />);

    expect(screen.getByText('bot 心跳已中断')).toBeInTheDocument();
    expect(screen.queryByText('已写入，等 bot 确认生效…')).toBeNull();
  });

  it('丢弃清掉全部草稿，各分区的计数一起归零', () => {
    platformConsoleStore('qq').setState({ draft: { allowed_groups: [101] } });
    render(<PlatformTab />);

    fireEvent.click(screen.getByRole('button', { name: '丢弃' }));

    expect(platformConsoleStore('qq').getState().draft).toEqual({});
    expect(header('信道')).not.toHaveTextContent('待应用');
  });
});

describe('页头的交代', () => {
  it('应用的警告和认账的结论连成一句', () => {
    platformConsoleStore('qq').setState({ notice: '原文认不出，整份文件重写了', confirmation: 'unconfirmed' });
    const view = render(<PlatformTab />);
    expect(screen.getByText(`原文认不出，整份文件重写了；${UNCONFIRMED}`)).toBeInTheDocument();

    view.unmount();
    platformConsoleStore('qq').setState({ notice: '', confirmation: 'unconfirmed' });
    render(<PlatformTab />);

    expect(screen.getByText(UNCONFIRMED)).toBeInTheDocument();
  });

  it('认账了就只剩应用时的警告，没有警告时一句都不摆', () => {
    platformConsoleStore('qq').setState({ notice: '原文认不出，整份文件重写了', confirmation: 'confirmed' });
    const view = render(<PlatformTab />);
    expect(screen.getByText('原文认不出，整份文件重写了')).toBeInTheDocument();

    view.unmount();
    platformConsoleStore('qq').setState({ notice: '', confirmation: 'confirmed' });
    render(<PlatformTab />);

    expect(screen.queryByText(/重写/)).toBeNull();
  });

  it('「应用」失败在页头报，读取失败和解析失败各是一条 alert', () => {
    const broken = live();
    platformConsoleStore('qq').setState({
      applyError: '写入失败：403 没有权限',
      loadError: '读取配置失败',
      live: { ...broken, state: { ...broken.state!, parse_error: 'line 3: bad indent' } },
    });
    render(<PlatformTab />);

    expect(screen.getAllByRole('alert').map((node) => node.textContent)).toEqual([
      '写入失败：403 没有权限',
      '读取配置失败',
      '平台配置解析失败：line 3: bad indent。bot 仍在用上一份有效配置。请先修复原文件语法；本页不会覆盖损坏文件。',
    ]);
  });

  it('「bot 还没上报」排在连接卡前面，先交代整页为什么空着', () => {
    platformConsoleStore('qq').setState({ live: { published: false, file: { exists: false, mtime_ns: 0, size: 0 } } });
    render(<PlatformTab />);

    const reason = screen.getByText(NOT_PUBLISHED);
    const card = screen.getByRole('heading', { name: '连接' });

    expect(reason.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe('跟着实况刷新', () => {
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });

  /** 记下每个请求的路径，供断言各拉了几次。 */
  const stubBackend = (): string[] => {
    const seen: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      seen.push(url);
      if (url.endsWith('/config/raw')) return json({ content: '', exists: false });
      if (url.endsWith('/config/state')) return json({ published: false, applied: false, file: {}, state: null, platforms: [] });
      return json({});
    }));
    return seen;
  };

  const countOf = (seen: readonly string[], leaf: string) => seen.filter((url) => url.endsWith(leaf)).length;

  afterEach(() => { vi.useRealTimers(); });

  it('进页各拉一次，之后每 15 秒再拉一次；本平台那份和全局那份都跟着读', async () => {
    const seen = stubBackend();
    useSettingsStore.setState({ adminToken: 'fixture-token' });
    vi.useFakeTimers();

    render(<PlatformTab />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(countOf(seen, '/v1/platforms/qq/config/state')).toBe(1);
    expect(countOf(seen, '/v1/features/config/state')).toBe(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });

    expect(countOf(seen, '/v1/platforms/qq/config/state')).toBe(2);
    expect(countOf(seen, '/v1/features/config/state')).toBe(2);
  });
});
