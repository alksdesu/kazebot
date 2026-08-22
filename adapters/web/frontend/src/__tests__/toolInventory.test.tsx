// 工具清单列的是全部工具，不只是 tools/ 目录里那二十个。
// 界面上看不见的工具，既勾不到授权，也没人想得起来它存在。
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ToolsSettingsRightPanel } from '../components/settings/panels/SettingsContextPanels';
import { ToolsSettingsPage } from '../components/settings/pages/ToolsSettingsPage';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

const CATALOG = [
  { name: 'write_file', source: 'builtin', editable: false, description: '写文件', input_schema: {}, has_spec: true },
  { name: 'save_memory', source: 'plugin', editable: false, description: '存一条记忆', input_schema: {}, has_spec: true },
  { name: 'exa_search', source: 'external', editable: true, description: '语义搜索', input_schema: {}, timeout_sec: 30, has_spec: true, file: 'tools/exa_search.py' },
];

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function stubCatalog() {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    // 同一页里自动审批那块自己也要一份名单，两个端点同源但形状不同。
    if (url.includes('all-tool-names')) return jsonResponse(CATALOG.map((tool) => tool.name));
    if (url.includes('/tools')) return jsonResponse(CATALOG);
    return jsonResponse([]);
  }));
}

async function renderInventory() {
  stubCatalog();
  render(<ToolsSettingsPage />);
  await screen.findByRole('button', { name: /exa_search/ });
}

const toolRow = (name: string) => screen.getByRole('button', { name: new RegExp(name) });

describe('工具清单', () => {
  beforeEach(() => {
    localStorage.clear();
    useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true, rightPanelOpen: false });
    useSettingsSelectionStore.setState({ selectedTool: null });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('内置和插件工具跟外部工具排在同一张表里', async () => {
    await renderInventory();

    for (const name of ['write_file', 'save_memory', 'exa_search']) {
      expect(toolRow(name)).toBeInTheDocument();
    }
  });

  it('每一行标出来源，改不了的标只读', async () => {
    await renderInventory();

    expect(within(toolRow('write_file')).getByText('内置')).toBeInTheDocument();
    expect(within(toolRow('save_memory')).getByText('插件')).toBeInTheDocument();
    expect(within(toolRow('exa_search')).getByText('外部')).toBeInTheDocument();
    expect(within(toolRow('write_file')).getByText('只读')).toBeInTheDocument();
    expect(within(toolRow('exa_search')).queryByText('只读')).toBeNull();
  });

  it('按来源筛，插件跟内置算一档 —— 对使用者都是改不了的那类', async () => {
    await renderInventory();

    fireEvent.click(screen.getByRole('button', { name: '内置' }));

    expect(screen.getByRole('button', { name: /write_file/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save_memory/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /exa_search/ })).toBeNull();
  });

  it('按关键字筛的时候描述也算数', async () => {
    await renderInventory();

    fireEvent.change(screen.getByLabelText('筛选工具'), { target: { value: '记忆' } });

    expect(screen.getByRole('button', { name: /save_memory/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /write_file/ })).toBeNull();
  });

  it('删不掉内置工具，按钮就不该是可点的', async () => {
    await renderInventory();

    fireEvent.click(toolRow('write_file'));
    await waitFor(() => expect(useSettingsSelectionStore.getState().selectedTool?.name).toBe('write_file'));

    expect(screen.getByRole('button', { name: '删除选中工具' })).toBeDisabled();
  });

  it('选中外部工具时删除可用', async () => {
    await renderInventory();

    fireEvent.click(toolRow('exa_search'));
    await waitFor(() => expect(useSettingsSelectionStore.getState().selectedTool?.name).toBe('exa_search'));

    expect(screen.getByRole('button', { name: '删除选中工具' })).toBeEnabled();
  });
});

describe('工具右栏', () => {
  beforeEach(() => {
    useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('内置工具不摆编辑器，也不去要它的源码', async () => {
    // 要了只会换回一句 409：这些工具根本没有 tools/ 下的文件。
    const fetchMock = vi.fn(async () => jsonResponse({ content: '' }));
    vi.stubGlobal('fetch', fetchMock);
    useSettingsSelectionStore.setState({ selectedTool: CATALOG[0] as never });

    render(<ToolsSettingsRightPanel />);

    expect(screen.queryByRole('button', { name: '保存' })).toBeNull();
    expect(screen.getByText(/改它要改仓库代码/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('外部工具照旧能编辑', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ content: 'SPEC = {}\n' })));
    useSettingsSelectionStore.setState({ selectedTool: CATALOG[2] as never });

    render(<ToolsSettingsRightPanel />);

    expect(await screen.findByRole('button', { name: '保存' })).toBeInTheDocument();
  });
});
