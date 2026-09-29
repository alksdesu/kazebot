// 节点授权。这一页显示的是权限边界，勾着却不生效比勾不上更糟 ——
// 白名单里写错的名字注入时是静默忽略的，界面不说就没人知道那一条从来没生效过。
// 每行保存完都重读整页，两行接连保存时只能认最后发出的那一次。
// 请求发出去之后换了 token，这一趟的结果不能落到新会话的页面上。
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAllToolNames,
  getEffectiveTools,
  getNodeRaw,
  getNodes,
  updateNodeRaw,
  type AdminNode,
  type RawSaveResult,
  type RawText,
  type EffectiveTool,
} from '../api/supervisorClient';
import { NodeGrantsSection } from '../components/settings/pages/NodeGrantsSection';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getAllToolNames: vi.fn(),
  getEffectiveTools: vi.fn(),
  getNodeRaw: vi.fn(),
  getNodes: vi.fn(),
  updateNodeRaw: vi.fn(),
}));

const ORCHESTRATOR: AdminNode = {
  id: 'qq.orchestrator',
  type: 'ai',
  tool_access: {
    mode: 'allowlist',
    allow: ['read_file', 'execute_command', 'ghost_tool', 'gemini_image', 'clonoth_debug'],
  },
  delegate_targets: ['bootstrap.executor'],
};

const INTENT: AdminNode = { id: 'qq.intent', type: 'ai', tool_access: { mode: 'none' } };

// 注册表里没有 ghost_tool —— 白名单写了个不存在的名字，正是要被标出来的那一条。
const REGISTERED = ['clonoth_debug', 'execute_command', 'gemini_image', 'read_file', 'write_file'];

const row = (name: string, over: Partial<EffectiveTool> = {}): EffectiveTool => ({
  name, registered: true, external: false, guarded: null, gated: '', ...over,
});

const EFFECTIVE: EffectiveTool[] = [
  row('read_file'),
  row('execute_command'),
  row('ghost_tool', { registered: false }),
  row('gemini_image', { gated: '渠道未配置 model，构建工具表时会被摘掉' }),
  row('clonoth_debug', { external: true, guarded: false }),
];

beforeEach(() => {
  vi.clearAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  vi.mocked(getNodes).mockResolvedValue([ORCHESTRATOR, INTENT]);
  vi.mocked(getAllToolNames).mockResolvedValue(REGISTERED);
  vi.mocked(getEffectiveTools).mockResolvedValue({
    node_id: 'qq.orchestrator', mode: 'allowlist', dead_names: ['ghost_tool'], tools: EFFECTIVE,
  });
});

const nodeRow = (id: string) =>
  screen.getByRole('heading', { name: id }).closest('li') as HTMLElement;

const expand = async (id: string) => {
  await screen.findByRole('heading', { name: id });
  fireEvent.click(screen.getByRole('heading', { name: id }));
  return nodeRow(id);
};

describe('某个节点授权了哪些工具', () => {
  it('折叠着也说得出模式和条数', async () => {
    render(<NodeGrantsSection />);
    await screen.findByRole('heading', { name: 'qq.orchestrator' });

    const orchestrator = within(nodeRow('qq.orchestrator'));
    expect(orchestrator.getByText('按勾选给')).toBeTruthy();
    expect(orchestrator.getByText('5 个工具')).toBeTruthy();

    const intent = within(nodeRow('qq.intent'));
    expect(intent.getByText('不给工具')).toBeTruthy();
    expect(intent.getByText('无')).toBeTruthy();
  });

  it('展开后勾中的就是白名单里那几个', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('qq.orchestrator'));

    for (const name of ['read_file', 'execute_command', 'gemini_image']) {
      const cell = item.getByText(name).closest('div') as HTMLElement;
      expect(within(cell).getByRole('checkbox')).toBeChecked();
    }
  });

  it('注册表里有但没授权的工具摆出来且没勾', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('qq.orchestrator'));

    const cell = item.getByText('write_file').closest('div') as HTMLElement;
    expect(within(cell).getByRole('checkbox')).not.toBeChecked();
  });

  it('注册表里已经没有的名字照样列出来，否则再也勾不回去', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('qq.orchestrator'));

    expect(within(item.getByText('ghost_tool').closest('div') as HTMLElement).getByRole('checkbox'))
      .toBeChecked();
  });
});

describe('勾了不等于能用', () => {
  it('不存在的名字标红，并点名说注入时会被静默忽略', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('qq.orchestrator'));

    const note = await item.findByText(/这些名字不存在/);
    expect(note).toHaveTextContent('ghost_tool');
    expect(note).toHaveClass('text-[var(--duties-danger)]');

    const cell = item.getByText('ghost_tool').closest('div') as HTMLElement;
    expect(within(cell).getByText('未注册')).toHaveClass('text-[var(--duties-danger)]');
  });

  it('只标红写错的那一个，配对的名字不受牵连', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('qq.orchestrator'));
    await item.findByText('未注册');

    const cell = item.getByText('read_file').closest('div') as HTMLElement;
    expect(within(cell).queryByText('未注册')).toBeNull();
  });

  it('渠道没设 model 被摘掉的生图工具也标出来', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('qq.orchestrator'));

    const note = await item.findByText(/能调但用不了/);
    expect(note).toHaveTextContent('gemini_image');
    const cell = item.getByText('gemini_image').closest('div') as HTMLElement;
    expect(within(cell).getByText('不可用')).toBeTruthy();
  });

  it('没声明 guard 的外部脚本说清服务端策略管不到', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('qq.orchestrator'));

    expect(await item.findByText(/「服务端策略」那一页管不到它们/)).toBeTruthy();
    const cell = item.getByText('clonoth_debug').closest('div') as HTMLElement;
    expect(within(cell).getByText('策略外')).toBeTruthy();
  });
});

describe('默认给得有多宽', () => {
  it('说清新建节点拿到的是全部工具', async () => {
    render(<NodeGrantsSection />);
    await screen.findByRole('heading', { name: 'qq.orchestrator' });

    expect(screen.getByText(/tool_access.mode: all/)).toBeTruthy();
  });

  it('点名现有节点手里的高危工具，数字跟着配置走', async () => {
    render(<NodeGrantsSection />);

    const line = await screen.findByText(/现在拿着/);
    expect(line).toHaveTextContent('qq.orchestrator');
    expect(line).toHaveTextContent('5 个工具');
    expect(line).toHaveTextContent('execute_command');
  });

  it('全部放开的节点即使没写 allow 也照样点名', async () => {
    // mode: all 的 allow 是空的，按条数算会漏掉最宽的那个节点。
    vi.mocked(getNodes).mockResolvedValue([
      { id: 'bootstrap.coder', type: 'ai', tool_access: { mode: 'all' } },
    ]);
    render(<NodeGrantsSection />);

    const line = await screen.findByText(/现在拿着/);
    expect(line).toHaveTextContent('bootstrap.coder');
    expect(line).toHaveTextContent('全部工具');
    expect(line).toHaveTextContent('remote_exec');
  });

  it('deny 掉的高危工具不再算在它头上', async () => {
    vi.mocked(getNodes).mockResolvedValue([{
      id: 'bootstrap.executor',
      type: 'ai',
      tool_access: { mode: 'all', deny: ['execute_command', 'remote_exec'] },
    }]);
    render(<NodeGrantsSection />);

    const line = await screen.findByText(/现在拿着/);
    expect(line).toHaveTextContent('manage_secret');
    expect(line).not.toHaveTextContent('remote_exec');
  });
});

describe('勾选在两种模式下是同一个意思', () => {
  // 之前复选框直接绑底层名单：allowlist 绑 allow、all 绑 deny，同一个勾正好相反。
  // 于是「默认全给 + 全选」= 一个工具都不能用，而界面看着像全开了。
  const EXECUTOR: AdminNode = {
    id: 'bootstrap.executor',
    type: 'ai',
    tool_access: { mode: 'all', deny: ['execute_command'] },
  };

  const cellFor = (item: ReturnType<typeof within>, name: string) =>
    within(item.getByText(name).closest('div') as HTMLElement).getByRole('checkbox');

  beforeEach(() => {
    vi.mocked(getNodes).mockResolvedValue([EXECUTOR]);
    vi.mocked(getEffectiveTools).mockResolvedValue({
      node_id: EXECUTOR.id, mode: 'all', dead_names: [], tools: [],
    });
    vi.mocked(getNodeRaw).mockResolvedValue({
      content: 'id: bootstrap.executor\ntool_access:\n  mode: all\n  deny:\n    - execute_command\n',
      hash: 'executor-1',
    });
    vi.mocked(updateNodeRaw).mockResolvedValue({ ok: true, hash: 'executor-2' });
  });

  it('默认全给时，禁用名单里的那个才是没勾的', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('bootstrap.executor'));

    expect(cellFor(item, 'execute_command')).not.toBeChecked();
    expect(cellFor(item, 'read_file')).toBeChecked();
    expect(cellFor(item, 'write_file')).toBeChecked();
  });

  it('取消勾选就是写进禁用名单', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('bootstrap.executor'));

    fireEvent.click(cellFor(item, 'read_file'));
    fireEvent.click(item.getByRole('button', { name: '保存授权' }));

    await vi.waitFor(() => expect(updateNodeRaw).toHaveBeenCalled());
    const [, , written, base] = vi.mocked(updateNodeRaw).mock.calls[0];
    expect(written).toContain('- execute_command');
    expect(written).toContain('- read_file');
    // 合并的底子是保存前现读的那一版，指纹得跟着它一起交回去。
    expect(base).toBe('executor-1');
  });

  it('「全部可调用」在默认全给下是清空禁用名单，不是禁掉全部', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('bootstrap.executor'));

    fireEvent.click(item.getByRole('button', { name: '让全部工具可调用' }));

    expect(cellFor(item, 'execute_command')).toBeChecked();
    expect(item.getByText(/5 个可调用/)).toBeTruthy();
  });

  it('「全部禁止」才是全关，而且说得出关了几个', async () => {
    render(<NodeGrantsSection />);
    const item = within(await expand('bootstrap.executor'));

    fireEvent.click(item.getByRole('button', { name: '禁止全部工具' }));

    expect(cellFor(item, 'read_file')).not.toBeChecked();
    expect(item.getByText(/0 个可调用/)).toBeTruthy();
  });

  it('筛选之后批量操作只动筛出来的那些', async () => {
    // 之前批量按钮直接把整份名单替换成筛选结果，搜一下再点全选，没显示的全没了。
    render(<NodeGrantsSection />);
    const item = within(await expand('bootstrap.executor'));

    fireEvent.change(item.getByLabelText('筛选 bootstrap.executor 的工具名'), { target: { value: 'read' } });
    fireEvent.click(item.getByRole('button', { name: /禁止筛出的 \d+ 个工具/ }));
    fireEvent.change(item.getByLabelText('筛选 bootstrap.executor 的工具名'), { target: { value: '' } });

    expect(cellFor(item, 'read_file')).not.toBeChecked();
    expect(cellFor(item, 'write_file')).toBeChecked();
  });
});

describe('两行接连保存', () => {
  const granted = (id: string, allow: string[]): AdminNode => ({
    id, type: 'ai', tool_access: { mode: 'allowlist', allow },
  });

  const BEFORE = [granted('qq.alpha', []), granted('qq.beta', [])];
  const AFTER = [granted('qq.alpha', ['read_file']), granted('qq.beta', ['write_file'])];
  // 存 alpha 引起的那次重读慢回来时，beta 还是保存前的空名单。
  const STALE = [granted('qq.alpha', ['read_file']), granted('qq.beta', [])];

  const checkbox = (id: string, tool: string) => within(
    within(nodeRow(id)).getByText(tool).closest('div') as HTMLElement,
  ).getByRole('checkbox');

  const saveRow = async (id: string, tool: string) => {
    const item = within(await expand(id));
    fireEvent.click(checkbox(id, tool));
    fireEvent.click(item.getByRole('button', { name: '保存授权' }));
  };

  beforeEach(() => {
    vi.mocked(getEffectiveTools).mockResolvedValue({
      node_id: 'qq.alpha', mode: 'allowlist', dead_names: [], tools: [],
    });
    vi.mocked(getNodeRaw).mockImplementation(async (_token, id) => ({ content: `id: ${id}\n`, hash: `${id}-1` }));
    vi.mocked(updateNodeRaw).mockResolvedValue({ ok: true, hash: 'saved' });
  });

  it('先发的那次重读慢回来，不把后保存的那一行打回旧值', async () => {
    let slow!: (nodes: AdminNode[]) => void;
    const held = new Promise<AdminNode[]>((resolve) => { slow = resolve; });
    vi.mocked(getNodes)
      .mockResolvedValueOnce(BEFORE)
      .mockReturnValueOnce(held)
      .mockResolvedValue(AFTER);

    render(<NodeGrantsSection />);
    await saveRow('qq.alpha', 'read_file');
    await waitFor(() => expect(getNodes).toHaveBeenCalledTimes(2));

    await saveRow('qq.beta', 'write_file');
    await waitFor(() => expect(getNodes).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(within(nodeRow('qq.beta')).getByText('1 个工具')).toBeTruthy());

    await act(async () => { slow(STALE); });

    expect(within(nodeRow('qq.beta')).getByText('1 个工具')).toBeTruthy();
    expect(checkbox('qq.beta', 'write_file')).toBeChecked();
    expect(within(nodeRow('qq.alpha')).getByText('1 个工具')).toBeTruthy();
  });
});

describe('保存到一半换了 token', () => {
  const ROTATED = 'rotated-token';
  const WRITE_FAILURE = '503 写不进节点文件';
  const ALPHA: AdminNode = { id: 'qq.alpha', type: 'ai', tool_access: { mode: 'allowlist', allow: [] } };

  /** 请求什么时候回来由测试说了算，好在它回来之前把 token 换掉。 */
  function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (reason: Error) => void } {
    let resolve!: (value: T) => void;
    let reject!: (reason: Error) => void;
    const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
  }

  const alphaRow = () => within(nodeRow('qq.alpha'));

  const note = () => alphaRow().getByRole('status').textContent;

  const startSave = async () => {
    const item = within(await expand('qq.alpha'));
    fireEvent.click(within(item.getByText('read_file').closest('div') as HTMLElement).getByRole('checkbox'));
    fireEvent.click(item.getByRole('button', { name: '保存授权' }));
  };

  const rotate = () => { act(() => { useSettingsStore.setState({ adminToken: ROTATED }); }); };

  beforeEach(() => {
    vi.mocked(getNodes).mockResolvedValue([ALPHA]);
    vi.mocked(getEffectiveTools).mockResolvedValue({
      node_id: ALPHA.id, mode: 'allowlist', dead_names: [], tools: [],
    });
    vi.mocked(getNodeRaw).mockResolvedValue({ content: 'id: qq.alpha\n', hash: 'alpha-1' });
    vi.mocked(updateNodeRaw).mockResolvedValue({ ok: true, hash: 'alpha-2' });
  });

  it('读回节点原文时 token 已经换掉，不拿旧 token 往回写', async () => {
    const read = deferred<RawText>();
    vi.mocked(getNodeRaw).mockReturnValueOnce(read.promise);
    render(<NodeGrantsSection />);
    await startSave();
    await waitFor(() => expect(getNodeRaw).toHaveBeenCalledOnce());

    rotate();
    await act(async () => { read.resolve({ content: 'id: qq.alpha\n', hash: 'alpha-1' }); });

    expect(updateNodeRaw).not.toHaveBeenCalled();
    expect(note()).toBe('有未保存的改动');
  });

  it('提前收手也要把「正在保存」收掉，不然这一行再也点不动', async () => {
    const read = deferred<RawText>();
    vi.mocked(getNodeRaw).mockReturnValueOnce(read.promise);
    render(<NodeGrantsSection />);
    await startSave();
    await waitFor(() => expect(alphaRow().getByRole('button', { name: '正在保存…' })).toBeDisabled());

    rotate();
    await act(async () => { read.resolve({ content: 'id: qq.alpha\n', hash: 'alpha-1' }); });

    expect(alphaRow().queryByRole('button', { name: '正在保存…' })).toBeNull();
    expect(alphaRow().getByRole('button', { name: '保存授权' })).toBeEnabled();
  });

  it('写完才换的 token，不提示已保存，也不拿旧 token 重读', async () => {
    const write = deferred<RawSaveResult>();
    vi.mocked(updateNodeRaw).mockReturnValueOnce(write.promise);
    render(<NodeGrantsSection />);
    await startSave();
    await waitFor(() => expect(updateNodeRaw).toHaveBeenCalledOnce());

    rotate();
    await waitFor(() => expect(getNodes).toHaveBeenCalledTimes(2));
    await act(async () => { write.resolve({ ok: true, hash: 'alpha-2' }); });

    expect(vi.mocked(getNodes).mock.calls.map(([token]) => token)).toEqual(['admin-token', ROTATED]);
    expect(note()).toBe('有未保存的改动');
  });

  it('写失败之前换了 token，不摆出这一趟的报错', async () => {
    const write = deferred<RawSaveResult>();
    vi.mocked(updateNodeRaw).mockReturnValueOnce(write.promise);
    render(<NodeGrantsSection />);
    await startSave();
    await waitFor(() => expect(updateNodeRaw).toHaveBeenCalledOnce());

    rotate();
    await act(async () => { write.reject(new Error(WRITE_FAILURE)); });

    expect(note()).toBe('有未保存的改动');
    expect(screen.queryByText(WRITE_FAILURE)).toBeNull();
  });

  it('退出登录后，上一个 token 读回来的节点清单不再套用', async () => {
    const list = deferred<AdminNode[]>();
    vi.mocked(getNodes).mockReturnValueOnce(list.promise);
    render(<NodeGrantsSection />);
    await waitFor(() => expect(getNodes).toHaveBeenCalledOnce());

    act(() => { useSettingsStore.setState({ adminToken: '' }); });
    await act(async () => { list.resolve([ALPHA]); });

    expect(screen.queryByRole('heading', { name: 'qq.alpha' })).toBeNull();
    expect(screen.getByText('正在读取…')).toBeTruthy();
  });
});

describe('白名单之外还有委派这一层', () => {
  it('列出这个节点能把活派给几个下游', async () => {
    render(<NodeGrantsSection />);
    await screen.findByRole('heading', { name: 'qq.orchestrator' });

    expect(within(nodeRow('qq.orchestrator')).getByText(/可派给 1 个下游节点/)).toBeTruthy();
  });

  it('说明白名单和 delegate_targets 各管一头', async () => {
    render(<NodeGrantsSection />);
    await screen.findByRole('heading', { name: 'qq.orchestrator' });

    expect(screen.getByText(/delegate_targets/)).toHaveTextContent('下游按下游自己那份 tool_access 去执行');
  });
});
