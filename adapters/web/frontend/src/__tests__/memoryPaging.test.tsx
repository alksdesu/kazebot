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

// 后端存完回的是整理过的整条：没写本名的落进 default，补上启用、扫描深度和时间戳。
const savedEntry = (payload: Record<string, unknown>) => ({
  enabled: true, priority: 0, scan_depth: 2, ...payload, book: payload.book || 'default', source: 'manual',
  created_at: '2026-10-28T01:00:00+00:00', updated_at: '2026-10-28T01:00:00+00:00',
});

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

// 页面上看见的那句由状态码接 detail 拼成。
const fails = (detail: string) => () =>
  new Response(JSON.stringify({ detail }), { status: 503, headers: { 'Content-Type': 'application/json' } });

type Reply = (() => Response) | null;

function stub({
  namespaces = [] as any[],
  entries = [] as any[],
  unreadable = [] as any[],
  conversations = [] as any[],
  removed = 0,
  messages = { total: 0, messages: [] } as { total: number; messages: unknown[] },
  replies = {} as { overview?: Reply[]; entries?: Reply[]; save?: Reply[]; clear?: Reply[]; reset?: Reply[] },
} = {}) {
  // 总览、条目、写入各走各的，得能单排某一路第几次的成败：按调用顺序取，null 是这一次照常回。
  const queued = {
    overview: [...(replies.overview || [])],
    entries: [...(replies.entries || [])],
    save: [...(replies.save || [])],
    clear: [...(replies.clear || [])],
    reset: [...(replies.reset || [])],
  };
  const take = (key: keyof typeof queued, body: unknown) =>
    (queued[key].length ? queued[key].shift() : null)?.() ?? jsonResponse(body);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method || 'GET';
    if (url.includes('/memory/overview')) return take('overview', { namespaces });
    if (url.includes('/entries')) {
      if (method === 'GET') return take('entries', { entries, unreadable_books: unreadable });
      if (method === 'DELETE') return jsonResponse({ ok: true });
      return take('save', { ok: true, entry: savedEntry(JSON.parse(String(init?.body))) });
    }
    if (url.includes('/admin/config/memory/')) return take('clear', { ok: true, removed });
    if (url.includes('/admin/config/runtime/raw')) {
      return jsonResponse({ content: '', hash: '', source: 'default', path: 'defaults/runtime.yaml', defaults: {}, effective: {} });
    }
    if (url.includes('/messages?')) return jsonResponse(messages);
    if (url.endsWith('/reset')) {
      return take('reset', { ok: true, old_session_id: 'sess-1', conversation_key: 'key-1', cleared_children: 0 });
    }
    if (url.includes('/admin/conversations')) return jsonResponse({ conversations });
    if (url.includes('/platforms/qq/account/scope')) return jsonResponse({ current_scope: '', bot_alive: false });
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
  vi.restoreAllMocks();
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
        {
          ...namespace(200, '人物档案', 'private'),
          kind: 'subject',
          owner: { kind: 'subject', label: '人物档案' },
        },
      ],
    });
    render(<MemoryPage />);
    await screen.findByText('群0');

    expect(screen.getByText('群组')).toBeInTheDocument();
    expect(screen.queryByText('人物')).toBeNull();

    clickNext('项');

    await screen.findByText('人0');
    // 第二页开头还是群组的尾巴，私聊和人物档案标题都要在。
    expect(screen.getByText('群组')).toBeInTheDocument();
    expect(screen.getByText('私聊')).toBeInTheDocument();
    expect(screen.getByText('人物')).toBeInTheDocument();
  });

  it('节点自己命名的记忆库单独成组，用目录名当名字，不落进通用', async () => {
    stub({
      namespaces: [
        namespace(1, '某群', 'group'),
        {
          ...namespace(2, '未知来源', 'group'),
          namespace: 'research_notes',
          key: 'research_notes',
          kind: 'memory_book',
          owner: { kind: 'unknown', label: '未知来源' },
        },
      ],
    });
    render(<MemoryPage />);

    const library = await screen.findByRole('button', { name: /research_notes/ });
    expect(screen.getByText('节点记忆库')).toBeInTheDocument();
    expect(screen.queryByText('通用')).toBeNull();
    expect(library.compareDocumentPosition(screen.getByText('节点记忆库')) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
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

    await screen.findByText('会话100（历史账号）');
    expect(screen.getByText(/其它账号/)).toBeInTheDocument();
  });

  it('预览照存档原样念多模态的正文，不念成 [object Object]', async () => {
    stub({
      conversations: [conversation(1)],
      messages: { total: 1, messages: [{ id: 'm1', role: 'user', content: [{ type: 'text', text: '看图' }] }] },
    });
    render(<MemoryPage />);
    await screen.findByText('会话1');

    fireEvent.click(screen.getByRole('button', { name: '查看' }));

    expect(await screen.findByText('共 1 条，显示最近 1 条')).toBeInTheDocument();
    expect(screen.getByText(/\[\{"type":"text","text":"看图"\}\]/)).toBeInTheDocument();
    expect(screen.queryByText(/object Object/)).toBeNull();
  });

  // 删掉了只是回执读不懂：报成没删，人会再删一次。
  it('删除上下文的回执读不懂：照删掉了交代，回执的事另起一行报', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    stub({ conversations: [conversation(1)], replies: { reset: [() => jsonResponse({ ok: true })] } });
    render(<MemoryPage />);
    await screen.findByText('会话1');

    fireEvent.click(screen.getByRole('button', { name: '删除' }));

    expect(await screen.findByText('已删除，下一条消息开始重新积累')).toBeInTheDocument();
    expect(screen.getByText('会话上下文已经删掉，可回执格式不对：缺 old_session_id')).toHaveAttribute('role', 'alert');
  });

  it('当前号一条会话都没有时照样说出来，不是被分页吞掉', async () => {
    stub({ conversations: Array.from({ length: 3 }, (_, index) => conversation(100 + index, false)) });
    render(<MemoryPage />);

    await screen.findByText('会话100（历史账号）');
    expect(screen.getByText('当前账号')).toBeInTheDocument();
    expect(screen.getByText('这个号还没有任何会话。')).toBeInTheDocument();
  });
});

describe('记忆页的那一句提示', () => {
  const sources = [namespace(1, '甲群', 'group'), namespace(2, '乙群', 'group')];

  const addEntry = async () => {
    await waitFor(() => expect(screen.getByLabelText('内容')).toBeEnabled());
    fireEvent.change(screen.getByLabelText('标识'), { target: { value: 'note_1' } });
    fireEvent.change(screen.getByLabelText('内容'), { target: { value: '记一笔' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
  };

  it('条目那一路读失败，换一份读成功就不再报那句', async () => {
    stub({ namespaces: sources, replies: { entries: [fails('条目读不回来')] } });
    render(<MemoryPage />);

    await pickSource('甲群');
    expect(await screen.findByText('503 条目读不回来')).toHaveAttribute('role', 'alert');

    await pickSource('乙群');

    await waitFor(() => expect(screen.queryByText('503 条目读不回来')).toBeNull());
  });

  it('「已添加」不被紧跟那次重读的失败盖掉，两路的原因合成一句另起一行报错', async () => {
    stub({
      namespaces: sources,
      replies: { entries: [null, fails('条目读不回来')], overview: [null, fails('总览读不回来')] },
    });
    render(<MemoryPage />);
    await pickSource('甲群');

    await addEntry();

    expect(await screen.findByText('已添加')).toBeInTheDocument();
    expect(screen.getByText('503 条目读不回来。503 总览读不回来')).toHaveAttribute('role', 'alert');
  });

  // 存上了只是回执读不懂：当成失败，人会再存一次；草稿留着，看着也像没存上。
  it('添加的回执读不懂：照添加了交代，草稿清掉，回执的事另起一行报', async () => {
    stub({ namespaces: sources, replies: { save: [() => jsonResponse({ ok: true })] } });
    render(<MemoryPage />);
    await pickSource('甲群');

    await addEntry();

    expect(await screen.findByText('已添加')).toBeInTheDocument();
    expect(screen.getByText('这条记忆已经存上，可回执格式不对：缺 entry。条目以重新读到的为准')).toHaveAttribute('role', 'alert');
    expect(screen.getByLabelText('内容')).toHaveValue('');
  });

  it('添加失败的原因报成错误，不当成提示', async () => {
    stub({ namespaces: sources, replies: { save: [fails('记忆目录只读')] } });
    render(<MemoryPage />);
    await pickSource('甲群');

    await addEntry();

    expect(await screen.findByText('503 记忆目录只读')).toHaveAttribute('role', 'alert');
    expect(screen.queryByText('已添加')).toBeNull();
  });

  it('「已清空 N 条」不被紧跟那次重读的失败盖掉，失败的原因另起一行报错', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    stub({ namespaces: sources, removed: 7, replies: { overview: [null, fails('总览读不回来')] } });
    render(<MemoryPage />);
    await pickSource('甲群');
    await waitFor(() => expect(screen.getByRole('button', { name: '清空' })).toBeEnabled());

    fireEvent.click(screen.getByRole('button', { name: '清空' }));

    expect(await screen.findByText('已清空 7 条')).toBeInTheDocument();
    expect(screen.getByText('503 总览读不回来')).toHaveAttribute('role', 'alert');
  });

  it('清空的回执读不懂：照清空了交代，不编一个条数', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    stub({ namespaces: sources, replies: { clear: [() => jsonResponse({ ok: true })] } });
    render(<MemoryPage />);
    await pickSource('甲群');
    await waitFor(() => expect(screen.getByRole('button', { name: '清空' })).toBeEnabled());

    fireEvent.click(screen.getByRole('button', { name: '清空' }));

    expect(await screen.findByText('已清空')).toBeInTheDocument();
    expect(screen.getByText('这一份记忆已经清空，可回执格式不对：缺 removed。删了几条没读到')).toHaveAttribute('role', 'alert');
    expect(screen.queryByText(/已清空 \d+ 条/)).toBeNull();
  });

  it('清空了最后一份，空栏说没有记忆，结果只在底下交代一次', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    stub({ namespaces: [namespace(1, '甲群', 'group')], removed: 3 });
    render(<MemoryPage />);
    await pickSource('甲群');
    await waitFor(() => expect(screen.getByRole('button', { name: '清空' })).toBeEnabled());
    stub({ namespaces: [], removed: 3 });

    fireEvent.click(screen.getByRole('button', { name: '清空' }));

    expect(await screen.findByText('还没有任何记忆。')).toBeInTheDocument();
    expect(screen.getAllByText('已清空 3 条')).toHaveLength(1);
  });

  it('总览读不回来就不说「还没有任何记忆」，只报读失败的原因', async () => {
    stub({ replies: { overview: [fails('总览读不回来')] } });
    render(<MemoryPage />);

    expect(await screen.findByText('503 总览读不回来')).toHaveAttribute('role', 'alert');
    expect(screen.queryByText('还没有任何记忆。')).toBeNull();
  });

  it('点另一个来源：操作结果和条目那一路的失败都清掉，总览那一路的留着', async () => {
    stub({
      namespaces: sources,
      replies: { entries: [null, fails('条目读不回来'), null], overview: [null, fails('总览读不回来')] },
    });
    render(<MemoryPage />);
    await pickSource('甲群');
    await addEntry();
    await screen.findByText('已添加');
    expect(screen.getByText('503 条目读不回来。503 总览读不回来')).toHaveAttribute('role', 'alert');

    await pickSource('乙群');

    expect(await screen.findByText('503 总览读不回来')).toHaveAttribute('role', 'alert');
    expect(screen.queryByText('已添加')).toBeNull();
    expect(screen.queryByText(/条目读不回来/)).toBeNull();
  });
});

// 读失败时手上是空表：说成「0 条」「还没有任何记忆」就是说它没有记忆，原因合在底下那句里，原地给重读。
describe('没读成不说成没有', () => {
  const sources = [namespace(1, '甲群', 'group')];

  it('条目没读成时不报条数，原地给重读，读到了照常列出', async () => {
    stub({ namespaces: sources, entries: [entry(1), entry(2)], replies: { entries: [fails('条目读不回来')] } });
    render(<MemoryPage />);

    await pickSource('甲群');
    expect(await screen.findByText('503 条目读不回来')).toHaveAttribute('role', 'alert');
    expect(screen.queryByText(/· 0 条/)).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '重新读取这一份' }));

    expect(await screen.findByText('第 1 条')).toBeInTheDocument();
    expect(screen.getByText(/· 2 条/)).toBeInTheDocument();
    expect(screen.queryByText('503 条目读不回来')).toBeNull();
    expect(screen.queryByRole('button', { name: '重新读取这一份' })).toBeNull();
  });

  it('条目读回来对不上形状也算没读成', async () => {
    stub({ namespaces: sources, replies: { entries: [() => jsonResponse({})] } });
    render(<MemoryPage />);

    await pickSource('甲群');

    expect(await screen.findByText('记忆条目列表格式不对：缺 entries')).toHaveAttribute('role', 'alert');
    expect(screen.queryByText(/· 0 条/)).toBeNull();
    expect(screen.getByRole('button', { name: '重新读取这一份' })).toBeInTheDocument();
  });

  it('来源没读成时原地给重读，读到了就列出来', async () => {
    stub({ namespaces: sources, replies: { overview: [fails('总览读不回来')] } });
    render(<MemoryPage />);

    expect(await screen.findByText('503 总览读不回来')).toHaveAttribute('role', 'alert');
    fireEvent.click(screen.getByRole('button', { name: '重新读取记忆来源' }));

    expect(await screen.findByRole('button', { name: /甲群/ })).toBeInTheDocument();
    expect(screen.queryByText('503 总览读不回来')).toBeNull();
    expect(screen.queryByRole('button', { name: '重新读取记忆来源' })).toBeNull();
  });

  it('来源读回来对不上形状，说格式不对，不说还没有任何记忆', async () => {
    stub({
      replies: { overview: [() => jsonResponse({ namespaces: [{ ...namespace(1, '甲群', 'group'), entry_count: '3' }] })] },
    });
    render(<MemoryPage />);

    expect(await screen.findByText('记忆来源列表格式不对：namespaces[0].entry_count 不是数字')).toHaveAttribute('role', 'alert');
    expect(screen.queryByText('还没有任何记忆。')).toBeNull();
  });
});

// 悄悄跳过的话，条数和本数少了一截，看着像那几本里的记忆没了；清空时它们也会被一起删掉。
describe('读不出来的记忆本', () => {
  const broken = [{ book: 'people', problem: '第 3 行第 5 列：mapping values are not allowed here' }];

  it('来源列表标出有几本读不了', async () => {
    stub({ namespaces: [{ ...namespace(2, '甲群', 'group'), unreadable_books: ['people'] }] });
    render(<MemoryPage />);

    const row = await screen.findByRole('button', { name: /甲群/ });

    expect(within(row).getByText('1 本读不了')).toBeInTheDocument();
  });

  it('条目区说出哪几本读不了、为什么，条数注明不含它们', async () => {
    stub({ namespaces: [namespace(1, '甲群', 'group')], entries: [entry(1)], unreadable: broken });
    render(<MemoryPage />);

    await pickSource('甲群');

    expect(await screen.findByText(/· 1 条（不含读不了的 1 本）/)).toBeInTheDocument();
    const notice = screen.getByText(/这几本读不出来/);
    expect(notice).toHaveAttribute('role', 'alert');
    expect(notice).toHaveTextContent('people.yaml（第 3 行第 5 列：mapping values are not allowed here）');
    expect(notice).toHaveTextContent('清空会连它们一起删掉');
  });

  it('都读得出来时不提', async () => {
    stub({ namespaces: [namespace(1, '甲群', 'group')], entries: [entry(1)] });
    render(<MemoryPage />);

    await pickSource('甲群');

    expect(await screen.findByText(/· 1 条$/)).toBeInTheDocument();
    expect(screen.queryByText(/这几本读不出来/)).toBeNull();
    expect(screen.queryByText(/本读不了/)).toBeNull();
  });
});

// 关键词是记忆本里手写的：写成单个字符串、混着数字都有，显示时照注入时的读法认，不能整页崩掉。
describe('手写的关键词', () => {
  it.each([
    ['单个词', '关键词：单个词'],
    [['甲', 3, ' 乙 ', ''], '关键词：甲、乙'],
  ])('照注入时的读法显示（%j）', async (keywords, shown) => {
    stub({ namespaces: [namespace(1, '甲群', 'group')], entries: [{ ...entry(1), keywords }] });
    render(<MemoryPage />);

    await pickSource('甲群');

    expect(await screen.findByText(shown)).toBeInTheDocument();
  });

  it('手写成数字的 id、写成映射的正文照样显示，删除时按 id 的文字找', async () => {
    stub({
      namespaces: [namespace(1, '甲群', 'group')],
      entries: [{ ...entry(1), id: 42, content: { text: '映射写的正文' } }],
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<MemoryPage />);

    await pickSource('甲群');

    expect(await screen.findByText('{"text":"映射写的正文"}')).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '删除' }));

    await waitFor(() => expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledWith(
      expect.stringContaining('/memory/ns-1/entries/main/42'), expect.objectContaining({ method: 'DELETE' }),
    ));
  });

  it.each([[{ a: 1 }], [42], [null]])('认不出的写法不崩，也不摆关键词那一行（%j）', async (keywords) => {
    stub({ namespaces: [namespace(1, '甲群', 'group')], entries: [{ ...entry(1), keywords }] });
    render(<MemoryPage />);

    await pickSource('甲群');

    expect(await screen.findByText('第 1 条')).toBeInTheDocument();
    expect(screen.queryByText(/关键词：/)).toBeNull();
  });
});

describe('到期时间', () => {
  it('有期限的标出哪天到期，过期的说明已不再注入，读不懂的照原样说出来', async () => {
    stub({
      namespaces: [namespace(1, '甲群', 'group')],
      entries: [
        { ...entry(1), expires_at: '2026-11-05T23:59:59+08:00', expiry_state: 'active' },
        { ...entry(2), expires_at: '2026-10-01T23:59:59+08:00', expiry_state: 'expired' },
        { ...entry(3), expires_at: '下周', expiry_state: 'unreadable' },
        { ...entry(4), expiry_state: 'none' },
      ],
    });
    render(<MemoryPage />);
    await pickSource('甲群');

    await screen.findByText('第 1 条');
    expect(screen.getByText('2026-11-05 23:59 到期')).toBeInTheDocument();
    expect(screen.getByText('已过期 · 不再注入，等待清理')).toBeInTheDocument();
    expect(screen.getByText('到期时间读不懂（下周），一直不会到期')).toBeInTheDocument();
    // 第 4 条没有到期时间，不摆标签。
    expect(screen.getAllByText(/ 到期$|^已过期|^到期时间读不懂/)).toHaveLength(3);
  });

  it('老后端不给 expiry_state：当成没有到期时间', async () => {
    stub({ namespaces: [namespace(1, '甲群', 'group')], entries: [entry(1)] });
    render(<MemoryPage />);
    await pickSource('甲群');

    await screen.findByText('第 1 条');
    expect(screen.queryByText(/ 到期$|^已过期|^到期时间读不懂/)).toBeNull();
  });

  it('填了到期日才带上 expires_at，没填的不带', async () => {
    stub({ namespaces: [namespace(1, '甲群', 'group')] });
    render(<MemoryPage />);
    await pickSource('甲群');
    await waitFor(() => expect(screen.getByLabelText('内容')).toBeEnabled());

    const add = async (id: string, expires: string) => {
      fireEvent.change(screen.getByLabelText('标识'), { target: { value: id } });
      fireEvent.change(screen.getByLabelText('内容'), { target: { value: '周六团建' } });
      fireEvent.change(screen.getByLabelText('关键词'), { target: { value: '团建' } });
      fireEvent.change(screen.getByLabelText('到期日'), { target: { value: expires } });
      fireEvent.click(screen.getByRole('button', { name: '添加' }));
      await waitFor(() => expect(screen.getByLabelText('标识')).toHaveValue(''));
    };
    await add('outing', '2026-11-08');
    await add('forever', '');

    const bodies = vi.mocked(globalThis.fetch).mock.calls
      .filter(([, init]) => init?.method === 'POST')
      .map(([, init]) => JSON.parse(String(init?.body)));
    expect(bodies[0].expires_at).toBe('2026-11-08');
    expect('expires_at' in bodies[1]).toBe(false);
  });

  it('后端回的到期状态对不上就算没读成', async () => {
    stub({ namespaces: [namespace(1, '甲群', 'group')], entries: [{ ...entry(1), expiry_state: 'soon' }] });
    render(<MemoryPage />);
    await pickSource('甲群');

    expect(await screen.findByRole('button', { name: '重新读取这一份' })).toBeInTheDocument();
    expect(screen.queryByText('第 1 条')).toBeNull();
  });
});

// 正则里的逗号被当成分隔符拆开，存下的两截都不成正则，召回时再也命中不了。
describe('添加时的关键词', () => {
  it('写成 /正则/ 的那段整段存成一个词，别的照逗号拆', async () => {
    stub({ namespaces: [namespace(1, '甲群', 'group')] });
    render(<MemoryPage />);
    await pickSource('甲群');
    await waitFor(() => expect(screen.getByLabelText('内容')).toBeEnabled());

    fireEvent.change(screen.getByLabelText('标识'), { target: { value: 'room' } });
    fireEvent.change(screen.getByLabelText('内容'), { target: { value: '住 302' } });
    fireEvent.change(screen.getByLabelText('关键词'), { target: { value: String.raw`/\d{1,3}/, 房间` } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));

    await screen.findByText('已添加');
    const save = vi.mocked(globalThis.fetch).mock.calls.find(([, init]) => init?.method === 'POST');
    expect(JSON.parse(String(save?.[1]?.body)).keywords).toEqual([String.raw`/\d{1,3}/`, '房间']);
  });
});
