// 图库列表的竞态：切页签、翻页、搜索接连触发时只认最后发出的那一次读，
// 慢回来的旧结果既不能顶掉眼前这一页的图和计数，也不能把旧错误报上来。
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import type { Sticker, StickerCounts, StickerPage } from '../api/supervisorClient';
import { StickersPage } from '../console/StickersPage';
import { useSettingsStore } from '../store/settingsStore';

// 同屏还挂着入库规则和看图渠道两块，各自去拉自己的数据，这里只看图库那一块。
vi.mock('../console/FeatureSection', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../console/FeatureSection')>()),
  StandaloneFeatureSection: () => null,
}));
vi.mock('../console/VisionChannelBlock', () => ({ VisionChannelBlock: () => null }));

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  listStickers: vi.fn(),
  acceptSticker: vi.fn(),
}));

// 停手 300ms 才真的去搜。
const DEBOUNCE_MS = 300;

const LIST_FAILURE = '503 图库读不回来';

// 待审超过一页，翻页控件才摆得出来。
const COUNTS: StickerCounts = { pending: 70, library: 3, discarded: 0, usable: 2, awaiting_caption: 1 };

const sticker = (name: string): Sticker => ({
  sha256: name, name, state: 'pending', source: 'group', tags: [], auto_tags: [], manual_tags: [],
  manual_override: false, caption_state: 'done', caption_error: '', width: 120, height: 120,
  animated: false, size: 2048, from_group: '', from_user: '', sent_count: 0, last_sent_at: 0,
  created_at: 0, usable: true,
});

const page = (names: string[], counts: Partial<StickerCounts> = {}): StickerPage => ({
  items: names.map(sticker),
  counts: { ...COUNTS, ...counts },
});

/** 一次由测试决定何时回来的请求。 */
function held<T>(): { promise: Promise<T>; resolve: (data: T) => void; reject: (reason: Error) => void } {
  let resolve!: (data: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const searchBox = () => screen.getByPlaceholderText('搜名字或标签');

const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }));

const asked = (index: number) => vi.mocked(api.listStickers).mock.calls[index][1];

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('图库只认最后发出的那次读', () => {
  it('切页签：慢回来的上一个页签不覆盖眼下这一页', async () => {
    const stale = held<StickerPage>();
    vi.mocked(api.listStickers).mockReturnValueOnce(stale.promise).mockResolvedValue(page(['在库图']));

    render(<StickersPage />);
    await waitFor(() => expect(api.listStickers).toHaveBeenCalledOnce());

    click('在库 0');
    expect(await screen.findByText('在库图')).toBeInTheDocument();
    expect(asked(1)).toEqual({ state: 'library', search: '', limit: 60, offset: 0 });

    await act(async () => { stale.resolve(page(['待审图'], { library: 99 })); });

    expect(screen.queryByText('待审图')).toBeNull();
    expect(screen.getByText('在库图')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '在库 3' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '在库 99' })).toBeNull();
  });

  it('翻页：慢回来的上一页不覆盖翻回来的这一页', async () => {
    vi.mocked(api.listStickers).mockResolvedValueOnce(page(['第一页的图']));
    render(<StickersPage />);
    await screen.findByText('第一页的图');

    const stale = held<StickerPage>();
    vi.mocked(api.listStickers).mockReturnValueOnce(stale.promise).mockResolvedValue(page(['翻回来的图']));

    click('下一页');
    await waitFor(() => expect(api.listStickers).toHaveBeenCalledTimes(2));
    click('上一页');
    expect(await screen.findByText('翻回来的图')).toBeInTheDocument();
    expect(asked(1)).toMatchObject({ offset: 60 });
    expect(asked(2)).toMatchObject({ offset: 0 });

    await act(async () => { stale.resolve(page(['第二页的图'], { pending: 99 })); });

    expect(screen.queryByText('第二页的图')).toBeNull();
    expect(screen.getByText('翻回来的图')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '待审 70' })).toBeInTheDocument();
  });

  it('搜索：慢回来的上一个词不覆盖新搜出来的', async () => {
    vi.useFakeTimers();
    const stale = held<StickerPage>();
    vi.mocked(api.listStickers)
      .mockResolvedValueOnce(page(['起手的图']))
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValue(page(['狗图']));

    render(<StickersPage />);
    await act(async () => { await vi.advanceTimersByTimeAsync(DEBOUNCE_MS); });

    fireEvent.change(searchBox(), { target: { value: '猫' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(DEBOUNCE_MS); });
    fireEvent.change(searchBox(), { target: { value: '狗' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(DEBOUNCE_MS); });

    expect(asked(1)).toMatchObject({ search: '猫' });
    expect(asked(2)).toMatchObject({ search: '狗' });
    expect(screen.getByText('狗图')).toBeInTheDocument();

    await act(async () => { stale.resolve(page(['猫图'], { pending: 99 })); });

    expect(screen.queryByText('猫图')).toBeNull();
    expect(screen.getByText('狗图')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '待审 70' })).toBeInTheDocument();
  });

  it('慢回来的那次失败，不把旧错误报到眼下这一页上', async () => {
    const stale = held<StickerPage>();
    vi.mocked(api.listStickers).mockReturnValueOnce(stale.promise).mockResolvedValue(page(['在库图']));

    render(<StickersPage />);
    await waitFor(() => expect(api.listStickers).toHaveBeenCalledOnce());

    click('在库 0');
    expect(await screen.findByText('在库图')).toBeInTheDocument();

    await act(async () => { stale.reject(new Error(LIST_FAILURE)); });

    expect(screen.queryByText(LIST_FAILURE)).toBeNull();
    expect(screen.getByText('在库图')).toBeInTheDocument();
  });
});

describe('图库的那一句提示', () => {
  it('这一档读失败，切到另一档读成功就不再报那句', async () => {
    vi.mocked(api.listStickers)
      .mockRejectedValueOnce(new Error(LIST_FAILURE))
      .mockResolvedValue(page(['在库图']));

    render(<StickersPage />);
    expect(await screen.findByText(LIST_FAILURE)).toBeInTheDocument();

    click('在库 0');

    expect(await screen.findByText('在库图')).toBeInTheDocument();
    expect(screen.queryByText(LIST_FAILURE)).toBeNull();
  });

  it('操作结果留到下一次操作，中间读成功不擦掉', async () => {
    vi.mocked(api.listStickers).mockResolvedValue(page(['待审图']));
    vi.mocked(api.acceptSticker).mockResolvedValue(sticker('待审图'));

    render(<StickersPage />);
    await screen.findByText('待审图');

    click('转正');
    expect(await screen.findByText('已转正，进候选了')).toBeInTheDocument();

    click('在库 3');
    await waitFor(() => expect(api.listStickers).toHaveBeenCalledTimes(3));
    expect(screen.getByText('已转正，进候选了')).toBeInTheDocument();

    vi.mocked(api.acceptSticker).mockRejectedValueOnce(new Error('转正没成'));
    click('转正');

    expect(await screen.findByText('转正没成')).toBeInTheDocument();
    expect(screen.queryByText('已转正，进候选了')).toBeNull();
  });

  it('操作完那次重读失败，结果和原因合成一句', async () => {
    vi.mocked(api.listStickers)
      .mockResolvedValueOnce(page(['待审图']))
      .mockRejectedValue(new Error(LIST_FAILURE));
    vi.mocked(api.acceptSticker).mockResolvedValue(sticker('待审图'));

    render(<StickersPage />);
    await screen.findByText('待审图');

    click('转正');

    expect(await screen.findByText(`已转正，进候选了。${LIST_FAILURE}`)).toBeInTheDocument();
  });

  it('操作做到一半换了号：不拿旧号的 token 重读，也不报这一次的结果', async () => {
    vi.mocked(api.listStickers).mockResolvedValue(page(['待审图']));
    const job = held<Sticker>();
    vi.mocked(api.acceptSticker).mockReturnValueOnce(job.promise);

    render(<StickersPage />);
    await screen.findByText('待审图');
    click('转正');

    await act(async () => { useSettingsStore.setState({ adminToken: 'another-token' }); });
    const reads = vi.mocked(api.listStickers).mock.calls.length;
    await act(async () => { job.resolve(sticker('待审图')); });

    expect(api.acceptSticker).toHaveBeenCalledOnce();
    expect(vi.mocked(api.listStickers).mock.calls.length).toBe(reads);
    expect(screen.queryByText('已转正，进候选了')).toBeNull();
  });

  it('重读回来之前换了号，不报上一号那次操作的结果', async () => {
    const reread = held<StickerPage>();
    vi.mocked(api.listStickers)
      .mockResolvedValueOnce(page(['待审图']))
      .mockReturnValueOnce(reread.promise)
      .mockResolvedValue(page(['待审图']));
    vi.mocked(api.acceptSticker).mockResolvedValue(sticker('待审图'));

    render(<StickersPage />);
    await screen.findByText('待审图');
    click('转正');
    await waitFor(() => expect(api.listStickers).toHaveBeenCalledTimes(2));

    await act(async () => { useSettingsStore.setState({ adminToken: 'another-token' }); });
    await act(async () => { reread.resolve(page(['待审图'])); });

    expect(screen.queryByText('已转正，进候选了')).toBeNull();
  });

  it('操作失败时已经换了号，这一号也不用看上一号的报错', async () => {
    vi.mocked(api.listStickers).mockResolvedValue(page(['待审图']));
    const job = held<Sticker>();
    vi.mocked(api.acceptSticker).mockReturnValueOnce(job.promise);

    render(<StickersPage />);
    await screen.findByText('待审图');
    click('转正');

    await act(async () => { useSettingsStore.setState({ adminToken: 'another-token' }); });
    await act(async () => { job.reject(new Error('转正没成')); });

    expect(screen.queryByText('转正没成')).toBeNull();
  });

  it('退出登录之后，上一个号的那一页回来不往页面上套', async () => {
    const stale = held<StickerPage>();
    vi.mocked(api.listStickers).mockReturnValueOnce(stale.promise);

    render(<StickersPage />);
    await waitFor(() => expect(api.listStickers).toHaveBeenCalledOnce());

    await act(async () => { useSettingsStore.setState({ adminToken: '' }); });
    await act(async () => { stale.resolve(page(['幽灵图'])); });

    expect(screen.queryByText('幽灵图')).toBeNull();
    expect(screen.getByRole('button', { name: '在库 0' })).toBeInTheDocument();
  });

  it('退出登录之后，上一个号那次读失败也不报出来', async () => {
    const stale = held<StickerPage>();
    vi.mocked(api.listStickers).mockReturnValueOnce(stale.promise);

    render(<StickersPage />);
    await waitFor(() => expect(api.listStickers).toHaveBeenCalledOnce());

    await act(async () => { useSettingsStore.setState({ adminToken: '' }); });
    await act(async () => { stale.reject(new Error(LIST_FAILURE)); });

    expect(screen.queryByText(LIST_FAILURE)).toBeNull();
  });
});
