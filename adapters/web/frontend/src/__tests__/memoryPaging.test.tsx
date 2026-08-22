// 记忆页的三个列表都随使用量无限变长：来源多了左栏能把整页顶到一千多像素，
// 右边却还是空的。分页守住高度，筛选守住「几十个来源里找那一个」。
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Pager } from '../components/settings/pages/settingsControls';
import { MemoryPage } from '../console/MemoryPage';
import { useSettingsStore } from '../store/settingsStore';

const namespace = (index: number, label: string, kind: 'group' | 'private') => ({
  key: `ns-${index}`,
  namespace: `ns-${index}`,
  kind: 'conversation',
  subject: '',
  entry_count: index,
  books: [],
  updated_at: '',
  owner: { kind, label },
});

const conversation = (index: number, currentAccount = true) => ({
  session_id: `sess-${index}`,
  conversation_key: `key-${index}`,
  channel: 'qq',
  bytes: 1024,
  updated_at: 0,
  owner: { kind: 'group', label: `会话${index}` },
  current_account: currentAccount,
});

const entry = (index: number) => ({
  id: `mem_${index}`,
  book: 'main',
  content: `第 ${index} 条`,
  keywords: [],
  constant: false,
  source: 'auto',
});

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function stub({ namespaces = [] as any[], entries = [] as any[], conversations = [] as any[] } = {}) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/memory/overview')) return jsonResponse({ namespaces });
    if (url.includes('/entries')) return jsonResponse({ entries });
    if (url.includes('/admin/conversations')) return jsonResponse({ conversations });
    if (url.includes('/admin/qq/scope')) return jsonResponse({ current_scope: '', bot_alive: false });
    return jsonResponse({});
  }));
}

const pickSource = async (label: string) =>
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(label) }));

// 左栏、右栏、会话块各有一条分页栏，按它旁边的区间文字认人。
const pagerFor = (unit: string) => screen.getByText(new RegExp(`共 \\d+ ${unit}`)).closest('div') as HTMLElement;
const clickNext = (unit: string) => fireEvent.click(within(pagerFor(unit)).getByRole('button', { name: '下一页' }));

beforeEach(() => {
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('翻页条', () => {
  it('装得下一页时整条不出现 —— 一排点不动的按钮只是占地方', () => {
    const { container } = render(
      <Pager offset={0} onOffset={() => undefined} pageSize={10} total={10} unit="项" />,
    );

    expect(container.firstChild).toBeNull();
  });

  it('报的是当前页的区间，不是页码', () => {
    render(<Pager offset={10} onOffset={() => undefined} pageSize={10} total={23} unit="项" />);

    expect(screen.getByText('第 11–20 项，共 23 项')).toBeInTheDocument();
  });

  it('末页的区间截到总数，不会报出不存在的项', () => {
    render(<Pager offset={20} onOffset={() => undefined} pageSize={10} total={23} unit="项" />);

    expect(screen.getByText('第 21–23 项，共 23 项')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();
  });

  it('首页禁「上一页」', () => {
    render(<Pager offset={0} onOffset={() => undefined} pageSize={10} total={23} unit="项" />);

    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '下一页' })).toBeEnabled();
  });

  it('翻页按整页走', () => {
    const onOffset = vi.fn();
    render(<Pager offset={10} onOffset={onOffset} pageSize={10} total={23} unit="项" />);

    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    expect(onOffset).toHaveBeenCalledWith(20);
    fireEvent.click(screen.getByRole('button', { name: '上一页' }));
    expect(onOffset).toHaveBeenCalledWith(0);
  });
});

describe('来源列表', () => {
  const many = Array.from({ length: 23 }, (_, index) => namespace(index, `群${index}`, 'group'));

  it('一页只放十项，剩下的收在翻页条后面', async () => {
    stub({ namespaces: many });
    render(<MemoryPage />);

    await screen.findByText('群0');
    expect(screen.queryByText('群10')).toBeNull();
    expect(screen.getByText('第 1–10 项，共 23 项')).toBeInTheDocument();
  });

  it('下一页换的是同一栏的内容，不是整页跳走', async () => {
    stub({ namespaces: many });
    render(<MemoryPage />);
    await screen.findByText('群0');

    clickNext('项');

    await screen.findByText('群10');
    expect(screen.queryByText('群0')).toBeNull();
  });

  it('筛选之后回到第一页 —— 停在第三页会看见空白', async () => {
    stub({ namespaces: many });
    render(<MemoryPage />);
    await screen.findByText('群0');
    clickNext('项');
    await screen.findByText('群10');

    fireEvent.change(screen.getByLabelText('按名字筛选来源'), { target: { value: '群1' } });

    // 群1、群10…群19：11 项，第一页仍是十项。
    expect(screen.getByText('群1')).toBeInTheDocument();
    expect(screen.getByText('第 1–10 项，共 11 项')).toBeInTheDocument();
  });

  it('筛不到就直说，不是留一栏空白', async () => {
    stub({ namespaces: many });
    render(<MemoryPage />);
    await screen.findByText('群0');

    fireEvent.change(screen.getByLabelText('按名字筛选来源'), { target: { value: '不存在的' } });

    expect(screen.getByText(/没有名字含「不存在的」的来源/)).toBeInTheDocument();
  });

  it('来源少于一页时不出现翻页条', async () => {
    stub({ namespaces: [namespace(1, '独苗群', 'group')] });
    render(<MemoryPage />);

    await screen.findByText('独苗群');
    expect(screen.queryByText(/共 \d+ 项/)).toBeNull();
  });

  it('组标题在每一页重新出现 —— 翻过去就不知道这一项属于哪一档了', async () => {
    stub({
      namespaces: [
        ...Array.from({ length: 12 }, (_, index) => namespace(index, `群${index}`, 'group')),
        ...Array.from({ length: 4 }, (_, index) => namespace(100 + index, `人${index}`, 'private')),
      ],
    });
    render(<MemoryPage />);
    await screen.findByText('群0');

    expect(screen.getByText('群组')).toBeInTheDocument();
    expect(screen.queryByText('人物')).toBeNull();

    clickNext('项');

    await screen.findByText('人0');
    // 第二页开头还是群组的尾巴，两个标题都要在。
    expect(screen.getByText('群组')).toBeInTheDocument();
    expect(screen.getByText('人物')).toBeInTheDocument();
  });
});

describe('记忆条目', () => {
  const many = Array.from({ length: 30 }, (_, index) => entry(index));

  it('一份里堆了三十条也只铺十二条', async () => {
    stub({ namespaces: [namespace(1, '话痨群', 'group')], entries: many });
    render(<MemoryPage />);

    await pickSource('话痨群');

    await screen.findByText('第 0 条');
    expect(screen.queryByText('第 12 条')).toBeNull();
    expect(screen.getByText('第 1–12 条，共 30 条')).toBeInTheDocument();
  });

  it('换一份看的时候退回第一页', async () => {
    stub({
      namespaces: [namespace(1, '甲群', 'group'), namespace(2, '乙群', 'group')],
      entries: many,
    });
    render(<MemoryPage />);
    await pickSource('甲群');
    await screen.findByText('第 0 条');
    clickNext('条');
    await screen.findByText('第 12 条');

    await pickSource('乙群');

    await waitFor(() => expect(screen.getByText('第 1–12 条，共 30 条')).toBeInTheDocument());
    expect(screen.getByText('第 0 条')).toBeInTheDocument();
  });
});

describe('会话上下文', () => {
  it('会话也分页', async () => {
    stub({ conversations: Array.from({ length: 20 }, (_, index) => conversation(index)) });
    render(<MemoryPage />);

    await screen.findByText('会话0');
    expect(screen.queryByText('会话15')).toBeNull();
    expect(screen.getByText('第 1–15 个，共 20 个')).toBeInTheDocument();
  });

  it('换过号时两段标题各自跟着自己那批走', async () => {
    stub({
      conversations: [
        // 当前号正好占满第一页，换号前那批才全落到第二页。
        ...Array.from({ length: 15 }, (_, index) => conversation(index)),
        ...Array.from({ length: 3 }, (_, index) => conversation(100 + index, false)),
      ],
    });
    render(<MemoryPage />);
    await screen.findByText('会话0');

    expect(screen.getByText('当前账号')).toBeInTheDocument();
    expect(screen.queryByText(/其它账号/)).toBeNull();

    clickNext('个');

    await screen.findByText('会话100');
    expect(screen.getByText(/其它账号/)).toBeInTheDocument();
  });

  it('当前号一条会话都没有时照样说出来，不是被分页吞掉', async () => {
    stub({ conversations: Array.from({ length: 3 }, (_, index) => conversation(100 + index, false)) });
    render(<MemoryPage />);

    await screen.findByText('会话100');
    expect(screen.getByText('当前账号')).toBeInTheDocument();
    expect(screen.getByText('这个号还没有任何会话。')).toBeInTheDocument();
  });
});
