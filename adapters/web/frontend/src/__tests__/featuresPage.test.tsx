// 机器人 › 全局默认：分区来自全局 schema、区块各自保存、列出各平台换没换上当前这份；图库那块摆在表情包图库页上。
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import type { ConfigSource, ConsoleSchema, FeatureAdoption, FeaturesLiveState } from '../api/supervisorClient';
import { SettingsSidebar } from '../components/settings/SettingsSidebar';
import { getSettingsTab } from '../components/settings/settingsTabs';
import { FEATURES_SCOPE } from '../console/configScope';
import { configStore } from '../console/consoleStore';
import { STICKER_LIBRARY_SECTION, StandaloneFeatureSection } from '../console/FeatureSection';
import { adoptionState } from '../console/FeaturesPage';
import { BOT_PAGES, MODEL_PAGES } from '../console/pages';
import { useSchemaStore } from '../console/schemaStore';
import { picksSection } from '../console/sectionPick';
import { confirmNavigation } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import { clearConsoleSchemas, FEATURES_SCHEMA, seedConsoleSchemas } from './consoleSchemaFixture';

vi.mock('../api/supervisorClient', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getFeaturesRaw: vi.fn(), getFeaturesState: vi.fn(), updateFeaturesRaw: vi.fn(), getFeaturesSchema: vi.fn(),
}));

const PAGE_LEAVE = '全局默认的「回复」还有没保存的修改，确定离开吗？';
const LIBRARY_LEAVE = '「入库」里还有没保存的修改，确定离开吗？';

// 快等阶段 250ms × 12 锁着按钮，之后后台 2000ms 一轮接着等，从写完算起满一分钟才下结论。
const QUICK_MS = 250 * 12;
const CONFIRM_MS = 61_000;
const LIVE_REFRESH_MS = 15_000;

const WAITING_NOTE = '已保存，正在等在跑的 bot 换上新文件…';
const UNCONFIRMED_NOTE = '已保存，但有在跑的 bot 没在 60 秒内换上新文件，「全局默认」页顶部列着是哪一个。';

// 键名 → 路径照后端快照取，写回时的换算和真实页面一致。
const PATHS = Object.fromEntries(Object.entries(FEATURES_SCHEMA.fields).map(([key, spec]) => [key, spec.path]));

const PLATFORMS: FeatureAdoption[] = [
  { id: 'qq', display_name: 'QQ · NapCat', published: true, applied: true },
  { id: 'telegram', display_name: 'Telegram', published: true, applied: false, reason: '还在用上一份全局设置' },
  { id: 'discord', display_name: 'Discord', published: false, applied: false },
];

const featuresLive = ({ parseError = '', platforms = PLATFORMS, sources = {} }: {
  parseError?: string;
  platforms?: FeatureAdoption[];
  sources?: Record<string, ConfigSource>;
} = {}): FeaturesLiveState => ({
  published: true,
  applied: true,
  file: { exists: true, mtime_ns: 1, size: 1 },
  platforms,
  state: {
    values: {
      reply_to_trigger: true,
      history_text_limit: 400,
      enable_echo: false,
      enable_preempt: false,
      sticker_auto_accept: false,
    },
    paths: PATHS,
    sources,
    parse_error: parseError,
  },
});

const DefaultsTab = () => {
  const Page = getSettingsTab('defaults').Page;
  return <Page />;
};

const renderLibrary = () => render(
  <StandaloneFeatureSection leaveMessage={LIBRARY_LEAVE} sectionId={STICKER_LIBRARY_SECTION} />,
);

const header = (name: string) => screen.getByRole('button', { name: new RegExp(`^${name}`) });
const sectionOf = (name: string) => header(name).closest('section')!;

/** 每次写回的全局文件原文。读原文时还回最近写进去的那份，没写过就是 baseRaw。 */
let written: string[] = [];
let baseRaw = '';

const signIn = () => useSettingsStore.setState({ adminToken: 'fixture-token' });

beforeEach(() => {
  written = [];
  baseRaw = 'version: 1\n';
  localStorage.clear();
  window.history.replaceState(null, '', '/');
  useSettingsStore.setState({ adminToken: '', isAuthenticated: true });
  useViewStore.setState({ viewMode: 'settings', activeSettingsTab: 'defaults', activeSettingsSection: '' });
  configStore(FEATURES_SCOPE).setState({
    live: featuresLive(), draft: {}, raw: '', rawExists: true, loading: false, saving: false, loadError: '', applyError: '', notice: '', confirmation: null,
  });
  seedConsoleSchemas();
  vi.mocked(api.getFeaturesState).mockImplementation(async () => featuresLive());
  vi.mocked(api.getFeaturesRaw).mockImplementation(async () => ({
    content: written.length ? written[written.length - 1] : baseRaw,
    exists: true,
  }));
  vi.mocked(api.updateFeaturesRaw).mockImplementation(async (_token, content) => {
    written.push(content);
    return { ok: true, warnings: [] };
  });
});

afterEach(() => {
  cleanup();
  clearConsoleSchemas();
  vi.useRealTimers();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe('全局默认页', () => {
  it('分区来自全局 schema，图库那块不在这一页', () => {
    render(<DefaultsTab />);

    for (const name of ['触发时机', '审批', '回复', '群互动', '输入处理', '表情包挑选', '队列与群历史']) {
      expect(header(name)).toHaveAttribute('aria-expanded', 'false');
    }
    expect(screen.queryByRole('button', { name: /^表情包图库/ })).toBeNull();
  });

  it('列出各平台换没换上当前这份，文字自带状态', () => {
    render(<DefaultsTab />);

    expect(screen.getByText('QQ · NapCat')).toBeInTheDocument();
    expect(screen.getByText('已换上当前这份')).toBeInTheDocument();
    expect(screen.getByText('还在用上一份全局设置')).toBeInTheDocument();
    expect(screen.getByText('bot 没在跑')).toBeInTheDocument();
  });

  it('后端没给出生效情况时直说', () => {
    configStore(FEATURES_SCOPE).setState({ live: featuresLive({ platforms: [] }) });
    render(<DefaultsTab />);

    expect(screen.getByText('后端没给出 bot 的生效情况。')).toBeInTheDocument();
  });

  it('全局文件写坏时说清楚 bot 还在用上一份', () => {
    configStore(FEATURES_SCOPE).setState({ live: featuresLive({ parseError: 'line 3: bad indent' }) });
    render(<DefaultsTab />);

    expect(screen.getByText(/全局设置解析失败：line 3: bad indent/)).toBeInTheDocument();
  });

  it('第一次读全局设置时说一声', () => {
    configStore(FEATURES_SCOPE).setState({ live: null, loading: true });
    render(<DefaultsTab />);

    expect(screen.getByText('正在读取全局设置…')).toBeInTheDocument();
  });

  it('布局还在路上时说一声，不拿空白冒充没有分区', () => {
    clearConsoleSchemas();
    signIn();
    // 请求永远不回，页面停在读取中。
    vi.mocked(api.getFeaturesSchema).mockReturnValue(new Promise<ConsoleSchema>(() => undefined));
    render(<DefaultsTab />);

    expect(screen.getByText('正在读取控制台布局…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^回复/ })).toBeNull();
  });

  it('深链指名的那一块展开', () => {
    useViewStore.setState({ activeSettingsSection: 'interaction' });
    render(<DefaultsTab />);

    expect(header('群互动')).toHaveAttribute('aria-expanded', 'true');
    expect(header('回复')).toHaveAttribute('aria-expanded', 'false');
  });

  it('展开状态记在这台浏览器上，和平台页分开记', () => {
    render(<DefaultsTab />);

    fireEvent.click(header('审批'));

    expect(JSON.parse(localStorage.getItem('console_sections_features')!)).toEqual({ approval: true });
    expect(localStorage.getItem('console_sections_qq')).toBeNull();
  });

  it('这一页的分区头没有摘要，名字里只剩标题和待保存计数', () => {
    configStore(FEATURES_SCOPE).setState({ draft: { reply_to_trigger: false } });
    render(<DefaultsTab />);

    expect(header('回复')).toHaveAccessibleName('回复 1 项待保存');
    expect(header('回复')).not.toHaveAttribute('aria-describedby');
    expect(header('审批')).toHaveAccessibleName('审批');
  });

  it('读取失败是一条 alert，已经画出来的东西照旧留着', () => {
    configStore(FEATURES_SCOPE).setState({ loadError: '读取全局设置失败' });
    render(<DefaultsTab />);

    expect(screen.getByRole('alert')).toHaveTextContent('读取全局设置失败');
    expect(header('回复')).toBeInTheDocument();
  });

  it('页面开着就跟着读，进页一次之后每 15 秒一次', async () => {
    signIn();
    vi.useFakeTimers();
    render(<DefaultsTab />);

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(api.getFeaturesState).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(LIVE_REFRESH_MS); });

    expect(api.getFeaturesState).toHaveBeenCalledTimes(2);
  });
});

describe('一个平台换没换上', () => {
  const platform = (over: Partial<FeatureAdoption>): FeatureAdoption => ({
    id: 'qq', display_name: 'QQ', published: true, applied: true, ...over,
  });

  it('bot 没在跑是灰的，带上后端给的原因', () => {
    expect(adoptionState(platform({ published: false, applied: false }))).toEqual({ tone: 'idle', text: 'bot 没在跑' });
    expect(adoptionState(platform({ published: false, applied: false, reason: '还没上报过' }))).toEqual({
      tone: 'idle', text: '还没上报过',
    });
  });

  it('心跳断了不算换上，哪怕上次报的是已换上', () => {
    expect(adoptionState(platform({ stale: true }))).toEqual({ tone: 'halt', text: 'bot 心跳已中断，不算在内' });
  });

  it('换上了是绿的', () => {
    expect(adoptionState(platform({}))).toEqual({ tone: 'live', text: '已换上当前这份' });
  });

  it('还没换上是红的，有原因就说原因', () => {
    expect(adoptionState(platform({ applied: false }))).toEqual({ tone: 'halt', text: '还没换上当前这份' });
    expect(adoptionState(platform({ applied: false, reason: '文件读不了' }))).toEqual({ tone: 'halt', text: '文件读不了' });
  });
});

describe('区块各自保存', () => {
  it('只写回这一块摆出来的键，别的块的改动留着', async () => {
    signIn();
    configStore(FEATURES_SCOPE).setState({ draft: { reply_to_trigger: false, enable_echo: true } });
    render(<DefaultsTab />);
    expect(header('回复')).toHaveTextContent('1 项待保存');
    expect(header('群互动')).toHaveTextContent('1 项待保存');

    fireEvent.click(header('回复'));
    fireEvent.click(within(sectionOf('回复')).getByRole('button', { name: '保存' }));
    await within(sectionOf('回复')).findByText('已保存。');

    expect(written).toHaveLength(1);
    expect(written[0]).toContain('reply_to_trigger: false');
    expect(written[0]).not.toContain('echo');
    expect(configStore(FEATURES_SCOPE).getState().draft).toEqual({ enable_echo: true });
    expect(header('回复')).not.toHaveTextContent('待保存');
    expect(header('群互动')).toHaveTextContent('1 项待保存');
  });

  it('丢弃只丢这一块', () => {
    configStore(FEATURES_SCOPE).setState({ draft: { reply_to_trigger: false, enable_echo: true } });
    render(<DefaultsTab />);

    fireEvent.click(header('回复'));
    fireEvent.click(within(sectionOf('回复')).getByRole('button', { name: '丢弃' }));

    expect(configStore(FEATURES_SCOPE).getState().draft).toEqual({ enable_echo: true });
  });

  it('保存失败的原因留在这一块里，草稿不丢', async () => {
    signIn();
    vi.mocked(api.updateFeaturesRaw).mockRejectedValue(new Error('400 全局设置写不进去'));
    configStore(FEATURES_SCOPE).setState({ draft: { reply_to_trigger: false } });
    render(<DefaultsTab />);

    fireEvent.click(header('回复'));
    fireEvent.click(within(sectionOf('回复')).getByRole('button', { name: '保存' }));

    // 页面开着时才冒出来的错误，读屏不会自己去看。
    expect(await within(sectionOf('回复')).findByRole('alert')).toHaveTextContent('400 全局设置写不进去');
    expect(configStore(FEATURES_SCOPE).getState().draft).toEqual({ reply_to_trigger: false });
    expect(configStore(FEATURES_SCOPE).getState().applyError).toBe('');
  });

  it('字段旁是「默认 / 自定义」，回到默认保存之后全局文件里那一行没了', async () => {
    baseRaw = 'version: 1\noutput:\n  reply_to_trigger: false\n';
    const live = featuresLive({ sources: { reply_to_trigger: 'global' } });
    configStore(FEATURES_SCOPE).setState({ live });
    vi.mocked(api.getFeaturesState).mockResolvedValue(live);
    signIn();
    render(<DefaultsTab />);
    fireEvent.click(header('回复'));
    const toggle = within(sectionOf('回复')).getByRole('group', { name: '引用原消息的来源' });
    expect(within(toggle).getByRole('button', { name: '自定义' })).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(within(toggle).getByRole('button', { name: '默认' }));
    expect(header('回复')).toHaveTextContent('1 项待保存');
    fireEvent.click(within(sectionOf('回复')).getByRole('button', { name: '保存' }));
    await within(sectionOf('回复')).findByText('已保存。');

    expect(written).toEqual(['version: 1\n']);
  });
});

describe('保存之后那一句', () => {
  /** 保存成功，但在跑的 bot 一直不换上新文件。 */
  const neverAdopts = (warnings: string[] = []) => {
    vi.mocked(api.getFeaturesState).mockImplementation(async () => ({ ...featuresLive(), applied: false }));
    vi.mocked(api.updateFeaturesRaw).mockImplementation(async (_token, content) => {
      written.push(content);
      return { ok: true, warnings };
    });
  };

  const saveReply = (warnings: string[] = []) => {
    signIn();
    neverAdopts(warnings);
    configStore(FEATURES_SCOPE).setState({ draft: { reply_to_trigger: false } });
    render(<DefaultsTab />);
    fireEvent.click(header('回复'));
    vi.useFakeTimers();
    fireEvent.click(within(sectionOf('回复')).getByRole('button', { name: '保存' }));
  };

  it('还在等在跑的 bot 换文件时先这么说，等满了换成结论', async () => {
    saveReply();

    await act(async () => { await vi.advanceTimersByTimeAsync(QUICK_MS); });
    expect(within(sectionOf('回复')).getByText(WAITING_NOTE)).toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(CONFIRM_MS); });
    expect(within(sectionOf('回复')).getByText(UNCONFIRMED_NOTE)).toBeInTheDocument();
  });

  it('写回时的警告排在这一句前面', async () => {
    saveReply(['bot 没在跑，只校验了 YAML 语法']);

    await act(async () => { await vi.advanceTimersByTimeAsync(QUICK_MS); });
    expect(within(sectionOf('回复')).getByText(`bot 没在跑，只校验了 YAML 语法；${WAITING_NOTE}`)).toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(CONFIRM_MS); });
    expect(within(sectionOf('回复')).getByText(`bot 没在跑，只校验了 YAML 语法；${UNCONFIRMED_NOTE}`)).toBeInTheDocument();
  });

  it('去改别的块不会把这一块的结论抹掉', async () => {
    saveReply();
    await act(async () => { await vi.advanceTimersByTimeAsync(QUICK_MS + CONFIRM_MS); });
    expect(within(sectionOf('回复')).getByText(UNCONFIRMED_NOTE)).toBeInTheDocument();

    // 结论说的是上一次保存到底生没生效，人又去动别处并不会让它变得不确定。
    act(() => { configStore(FEATURES_SCOPE).getState().setDraft('enable_echo', true); });

    expect(within(sectionOf('回复')).getByText(UNCONFIRMED_NOTE)).toBeInTheDocument();
  });

  it('bot 认账了就只剩警告，没警告才是一句「已保存。」', async () => {
    signIn();
    vi.mocked(api.updateFeaturesRaw).mockImplementation(async (_token, content) => {
      written.push(content);
      return { ok: true, warnings: ['bot 没在跑，只校验了 YAML 语法'] };
    });
    configStore(FEATURES_SCOPE).setState({ draft: { reply_to_trigger: false } });
    render(<DefaultsTab />);

    fireEvent.click(header('回复'));
    fireEvent.click(within(sectionOf('回复')).getByRole('button', { name: '保存' }));

    expect(await within(sectionOf('回复')).findByText('bot 没在跑，只校验了 YAML 语法')).toBeInTheDocument();
    expect(configStore(FEATURES_SCOPE).getState().confirmation).toBe('confirmed');
  });
});

describe('离开前拦未保存的改动', () => {
  it('全局默认页只算这一页摆出来的键', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    configStore(FEATURES_SCOPE).setState({ draft: { sticker_auto_accept: true } });
    const view = render(<DefaultsTab />);

    // 图库那项在表情包图库页上改，没保存也不该拦着离开这一页。
    expect(confirmNavigation()).toBe(true);
    expect(confirm).not.toHaveBeenCalled();

    view.unmount();
    configStore(FEATURES_SCOPE).setState({ draft: { reply_to_trigger: false } });
    render(<DefaultsTab />);

    expect(confirmNavigation()).toBe(false);
    expect(confirm).toHaveBeenCalledWith(PAGE_LEAVE);
  });

  it('各块分开保存，那句话里得点名是哪几块', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    configStore(FEATURES_SCOPE).setState({ draft: { reply_to_trigger: false, enable_echo: true } });
    render(<DefaultsTab />);

    expect(confirmNavigation()).toBe(false);
    expect(confirm).toHaveBeenCalledWith('全局默认的「回复」「群互动」还有没保存的修改，确定离开吗？');
  });

  it('表情包图库页上的入库规则只拦图库那几项', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    configStore(FEATURES_SCOPE).setState({ draft: { reply_to_trigger: false } });
    const view = renderLibrary();

    expect(confirmNavigation()).toBe(true);
    expect(confirm).not.toHaveBeenCalled();

    view.unmount();
    configStore(FEATURES_SCOPE).setState({ draft: { sticker_auto_accept: true } });
    renderLibrary();

    expect(confirmNavigation()).toBe(false);
    expect(confirm).toHaveBeenCalledWith(LIBRARY_LEAVE);
  });
});

describe('表情包图库页上的入库规则', () => {
  it('摆的是全局 schema 里图库那一块，保存写进全局文件', async () => {
    signIn();
    configStore(FEATURES_SCOPE).setState({ draft: { sticker_auto_accept: true } });
    renderLibrary();
    expect(screen.getByText('入库')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /跳过人工筛选/ })).toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await screen.findByText('已保存。');

    expect(written).toHaveLength(1);
    expect(written[0]).toContain('auto_accept: true');
    expect(configStore(FEATURES_SCOPE).getState().draft).toEqual({});
  });

  it('布局拉不到时把原因摆出来', () => {
    useSchemaStore.setState({ schemas: {}, errors: { [FEATURES_SCOPE.id]: '读不到控制台布局：网关超时' } });
    renderLibrary();

    expect(screen.getByRole('alert')).toHaveTextContent('读不到控制台布局：网关超时');
  });

  it('读到过之后再读失败只在区块上面提一句，正在改的东西不从眼前消失', () => {
    configStore(FEATURES_SCOPE).setState({ loadError: '读取全局设置失败' });
    renderLibrary();

    const alert = screen.getByRole('alert');
    const block = screen.getByRole('heading', { name: '入库' });
    expect(alert).toHaveTextContent('读取全局设置失败');
    expect(alert.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole('checkbox', { name: /跳过人工筛选/ })).toBeInTheDocument();
  });

  it('一次都没读到时整块换成错误', () => {
    configStore(FEATURES_SCOPE).setState({ live: null, loadError: '读取全局设置失败' });
    renderLibrary();

    expect(screen.getByRole('alert')).toHaveTextContent('读取全局设置失败');
    expect(screen.queryByText('入库')).toBeNull();
  });

  it('页面开着就跟着读，进页一次之后每 15 秒一次', async () => {
    signIn();
    vi.useFakeTimers();
    renderLibrary();

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(api.getFeaturesState).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(LIVE_REFRESH_MS); });

    expect(api.getFeaturesState).toHaveBeenCalledTimes(2);
  });

  it('从侧栏搜过来才滚到这一块，平常打开这页不乱滚', () => {
    const scroll = vi.fn();
    // jsdom 没有这个方法，临时装一个看有没有被叫到。
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll });
    try {
      const view = renderLibrary();
      expect(scroll).not.toHaveBeenCalled();

      view.unmount();
      useViewStore.setState({ activeSettingsSection: STICKER_LIBRARY_SECTION });
      renderLibrary();

      expect(scroll).toHaveBeenCalledWith({ block: 'start' });
    } finally {
      delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    }
  });
});

describe('一份全局 schema 分摆两页', () => {
  it('每一块恰好摆在一页上，图库那块在表情包图库页', () => {
    const pages = [...BOT_PAGES, ...MODEL_PAGES].filter((page) => page.configScope === FEATURES_SCOPE);

    for (const section of FEATURES_SCHEMA.sections) {
      const owners = pages.filter((page) => picksSection(page.sections, section.id)).map((page) => page.id);
      expect(owners, section.id).toEqual([section.id === STICKER_LIBRARY_SECTION ? 'stickers' : 'defaults']);
    }
  });

  it('侧栏搜到图库那几项时指向表情包图库页，不挂在全局默认下面', () => {
    render(<SettingsSidebar />);

    fireEvent.change(screen.getByLabelText('搜索设置'), { target: { value: '跳过人工筛选' } });

    expect(screen.queryByRole('button', { name: '全局默认：表情包图库' })).toBeNull();
    // 分区和页同名，并进页那一行，同一个名字不上下列两遍。
    expect(screen.queryByRole('button', { name: '表情包图库：表情包图库' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '表情包图库' }));
    expect(useViewStore.getState().activeSettingsTab).toBe('stickers');
    expect(useViewStore.getState().activeSettingsSection).toBe(STICKER_LIBRARY_SECTION);
  });

  it('全局默认的分区和平台页里同名的那块都搜得到', () => {
    render(<SettingsSidebar />);

    fireEvent.change(screen.getByLabelText('搜索设置'), { target: { value: '跟复读' } });

    expect(screen.getByRole('button', { name: '全局默认：群互动' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'QQ · NapCat：群互动' })).toBeInTheDocument();
  });
});
