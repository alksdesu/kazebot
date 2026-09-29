// 绘图页的结构化字段写回：只重写改了的键，运营者手写的注释留着；原文坏了一个字节都不写。
// 保存的提示要活过紧跟着的那次重载，只有「刷新」清得掉。
// 分析节点那一栏：404 才当空文件，别的读失败要锁住；接连重读只认最后发出的那次。
// 写出去之后换了 token，这一趟的结果不能落到新会话的页面上。
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import yaml from 'js-yaml';

import * as api from '../api/supervisorClient';
import type { DrawtoolsBundle } from '../api/supervisorClient';
import { DrawtoolsSettingsPage } from '../components/settings/pages/DrawtoolsSettingsPage';
import { BLOCK_COMMENTS_DROPPED, WHOLE_FILE_REWRITTEN } from '../console/yamlRewrite';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  cleanupDrawtoolsAttachments: vi.fn(),
  getDrawtoolsBundle: vi.fn(),
  getNodeRaw: vi.fn(),
  initDrawtoolsConfigs: vi.fn(),
  updateDrawtoolsCharactersRaw: vi.fn(),
  updateDrawtoolsPromptRaw: vi.fn(),
  updateDrawtoolsSettingsRaw: vi.fn(),
  updateNodeRaw: vi.fn(),
}));

const RELOAD_FAILURE = '503 拉不到绘图配置';

const NODE_FAILURE = '503 读不到节点文件';

const NODE_YAML = 'id: draw.novelai_planner\nmodel: $ENV{DRAW_MODEL}\n';

const NODE_LOCKED = `读不到这份文件：${NODE_FAILURE}。点上面的「刷新」重读；读到之前不能在这里改，免得空白内容盖掉原文件。`;

const STALE_SETTINGS_YAML = 'api:\n  base_url: https://stale.example\n';

const EDITED_SETTINGS_YAML = 'api:\n  base_url: https://relay.example\n';

const SETTINGS_YAML = [
  '# NovelAI 绘图设置，注释是运营者自己写的，改字段不能把它们冲掉。',
  'api:',
  '  # 留空走官方站',
  '  base_url: "https://image.novelai.net"',
  '  api_key_env: NOVELAI_API_KEY',
  'generation:',
  '  request_delay_sec: 0',
  'params:',
  '  selected_preset_id: soft',
  '  presets:',
  '    - id: soft',
  '      name: 柔光',
  '',
].join('\n');

const CHARACTERS_YAML = [
  '# 角色标签库：外貌和服装都是手调过的。',
  'version: 1',
  '# 下面是角色清单',
  'characters:',
  '  - name: 初音未来',
  '    danbooru: hatsune_miku',
  '    appearance: long hair',
  '',
].join('\n');

// 删掉一个还剩一个，列表能就地删那一项，注释和另一项都不动。
const TWO_CHARACTERS_YAML = [
  '# 角色标签库：外貌和服装都是手调过的。',
  'version: 1',
  'characters:',
  '  - name: 初音未来',
  '    danbooru: hatsune_miku',
  '  - name: 镜音铃',
  '    danbooru: kagamine_rin',
  '',
].join('\n');

// 别名列表里夹着注释，一次删掉两条就只能整块重写。
const ALIASED_CHARACTERS_YAML = [
  'characters:',
  '  - name: 初音未来',
  '    aliases:',
  '      - 初音',
  '      # 下面这个是英文名',
  '      - miku',
  '      - hatsune',
  '    danbooru: hatsune_miku',
  '',
].join('\n');

const bundle = (settings = SETTINGS_YAML, characters = CHARACTERS_YAML): DrawtoolsBundle => ({
  settings: { content: settings, hash: 'settings-1', exists: true, path: 'config/drawtools/settings.yaml' },
  character_tags: { content: characters, hash: 'characters-1', exists: true, path: 'config/drawtools/character_tags.yaml' },
  prompts: {},
});

const serve = (data: DrawtoolsBundle): void => { vi.mocked(api.getDrawtoolsBundle).mockResolvedValue(data); };

/** 首读照常给，保存之后那一次由测试自己决定什么时候回来。 */
function holdReload(first = bundle()): { resolve: (data: DrawtoolsBundle) => void; reject: (reason: unknown) => void } {
  let handles!: { resolve: (data: DrawtoolsBundle) => void; reject: (reason: unknown) => void };
  const held = new Promise<DrawtoolsBundle>((resolve, reject) => { handles = { resolve, reject }; });
  vi.mocked(api.getDrawtoolsBundle).mockResolvedValueOnce(first).mockReturnValueOnce(held);
  return handles;
}

/** 一次由测试决定何时回来的读。 */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (reason: Error) => void } {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

/** 没有消息时整个 StatusText 不渲染，只能等它出现。 */
const statusText = async (): Promise<string> => (await screen.findByRole('status')).textContent || '';

const baseUrlBox = () => screen.getByPlaceholderText('https://image.novelai.net');

// 页上好几处都有 textarea 和「丢弃」，原始编辑器只能连着它那一块一起找。
const rawBlock = () => screen.getByRole('heading', { name: '原始配置编辑器' }).closest('section') as HTMLElement;

const rawEditor = () => within(rawBlock()).getByRole('textbox') as HTMLTextAreaElement;

const rawButton = (name: string) => within(rawBlock()).getByRole('button', { name });

const pickSection = (key: string) => {
  const select = screen.getByRole('option', { name: '绘图分析节点 YAML' }).closest('select') as HTMLSelectElement;
  fireEvent.change(select, { target: { value: key } });
};

const written = (writer: { mock: { calls: unknown[][] } }): string => String(writer.mock.calls[0][1]);

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  serve(bundle());
  vi.mocked(api.getNodeRaw).mockResolvedValue({ content: 'id: draw.novelai_planner\n', hash: 'planner-1' });
  vi.mocked(api.initDrawtoolsConfigs).mockResolvedValue({ ok: true, created: [] });
  vi.mocked(api.updateDrawtoolsSettingsRaw).mockResolvedValue({ ok: true, hash: 'settings-2' });
  vi.mocked(api.updateDrawtoolsCharactersRaw).mockResolvedValue({ ok: true, hash: 'characters-2' });
  vi.mocked(api.updateNodeRaw).mockResolvedValue({ ok: true, hash: 'planner-2' });
});

describe('绘图设置的结构化写回', () => {
  it('改一个 API 字段只动那一行，注释和其余键原样留着', async () => {
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));

    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });

    await waitFor(() => expect(api.updateDrawtoolsSettingsRaw).toHaveBeenCalledOnce());
    expect(written(vi.mocked(api.updateDrawtoolsSettingsRaw))).toBe(
      SETTINGS_YAML.replace('"https://image.novelai.net"', 'https://relay.example'),
    );
    expect(await statusText()).toBe('API 配置已保存。');
  });

  it('改角色标签库同样保注释', async () => {
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(screen.getByText('初音未来')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    fireEvent.blur(screen.getByPlaceholderText('hatsune_miku'), { target: { value: 'hatsune_miku_v4' } });

    await waitFor(() => expect(api.updateDrawtoolsCharactersRaw).toHaveBeenCalledOnce());
    expect(written(vi.mocked(api.updateDrawtoolsCharactersRaw))).toBe(
      CHARACTERS_YAML.replace('hatsune_miku', 'hatsune_miku_v4'),
    );
    expect(await statusText()).toBe('已保存角色字段：danbooru');
  });

  it('原文不是合法 YAML 就一个字节都不写', async () => {
    const broken = 'api:\n  base_url: "未闭合\ngeneration: [\n';
    const reason = (() => {
      try { yaml.load(broken); return ''; } catch (error) { return (error as Error).message; }
    })();
    serve(bundle(broken));

    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toBeInTheDocument());

    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });

    expect(await statusText()).toBe(`原文不是合法的 YAML，请先修好再改，原文件未修改：${reason}`);
    expect(api.updateDrawtoolsSettingsRaw).not.toHaveBeenCalled();
  });

  it('原文有认不出的写法只能整份重写，提示里如实交代', async () => {
    serve(bundle('# 会丢的注释\napi: {base_url: "https://image.novelai.net"}\n'));

    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));

    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });

    await waitFor(() => expect(api.updateDrawtoolsSettingsRaw).toHaveBeenCalledOnce());
    expect(written(vi.mocked(api.updateDrawtoolsSettingsRaw))).toBe('api:\n  base_url: https://relay.example\n');
    expect(await statusText()).toBe(`API 配置已保存。${WHOLE_FILE_REWRITTEN}。`);
  });

  it('提示本身没句号时补一个，两句不会粘成一句', async () => {
    serve(bundle(
      'params: {selected_preset_id: soft, presets: [{id: soft, name: 柔光}, {id: hard, name: 硬光}]}\n',
    ));

    render(<DrawtoolsSettingsPage />);
    await screen.findByRole('option', { name: '硬光' });

    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'hard' } });

    await waitFor(() => expect(api.updateDrawtoolsSettingsRaw).toHaveBeenCalledOnce());
    expect(await statusText()).toBe(`已切换默认预设：hard。${WHOLE_FILE_REWRITTEN}。`);
  });

  it('只有那一块重写时说的是块里的注释没了', async () => {
    serve(bundle(SETTINGS_YAML, ALIASED_CHARACTERS_YAML));
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(screen.getByText('初音未来')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    fireEvent.blur(screen.getByPlaceholderText('小名、英文名、昵称...'), { target: { value: '初音' } });

    await waitFor(() => expect(api.updateDrawtoolsCharactersRaw).toHaveBeenCalledOnce());
    expect(written(vi.mocked(api.updateDrawtoolsCharactersRaw))).toBe([
      'characters:',
      '  - name: 初音未来',
      '    aliases:',
      '      - 初音',
      '    danbooru: hatsune_miku',
      '',
    ].join('\n'));
    expect(await statusText()).toBe(`已保存角色字段：aliases。${BLOCK_COMMENTS_DROPPED}。`);
  });
});

describe('绘图设置的提示存活', () => {
  it('保存之后紧接着的重载不会把提示吃掉', async () => {
    const reload = holdReload();
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));

    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });
    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledTimes(2));
    await act(async () => { reload.resolve(bundle()); });

    expect(screen.getByRole('status').textContent).toBe('API 配置已保存。');
  });

  it('点「刷新」才把上一次的提示清掉', async () => {
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));
    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });
    expect(await statusText()).toBe('API 配置已保存。');

    fireEvent.click(screen.getByRole('button', { name: '刷新' }));

    await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
  });

  it('重载失败时把原因接在已有提示后面', async () => {
    const reload = holdReload();
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));

    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });
    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('status').textContent).toBe('API 配置已保存。');
    await act(async () => { reload.reject(new Error(RELOAD_FAILURE)); });

    expect(screen.getByRole('status').textContent).toBe(`API 配置已保存。${RELOAD_FAILURE}`);
  });

  it('新增角色之后紧接着的重载不会把提示吃掉', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('镜音铃');
    const reload = holdReload();
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(screen.getByText('初音未来')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: '+ 新增角色' }));
    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledTimes(2));
    await act(async () => { reload.resolve(bundle()); });

    expect(screen.getByRole('status').textContent).toBe('已新增角色：镜音铃');
  });

  it('删除角色之后紧接着的重载不会把提示吃掉', async () => {
    const twoCharacters = bundle(SETTINGS_YAML, TWO_CHARACTERS_YAML);
    const reload = holdReload(twoCharacters);
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(screen.getByText('镜音铃')).toBeInTheDocument());

    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[0]);
    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledTimes(2));
    await act(async () => { reload.resolve(twoCharacters); });

    expect(screen.getByRole('status').textContent).toBe('已删除角色：初音未来');
  });

  it('下一次读成功只去掉失败原因，新写下的结果照常显示', async () => {
    const reload = holdReload();
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));

    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });
    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledTimes(2));
    await act(async () => { reload.reject(new Error(RELOAD_FAILURE)); });
    expect(screen.getByRole('status').textContent).toBe(`API 配置已保存。${RELOAD_FAILURE}`);

    fireEvent.click(screen.getByRole('button', { name: '从 Example 初始化用户配置' }));

    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('用户配置已存在，无需创建。'));
  });

  it('点「刷新」把结果和读失败的原因一起清掉', async () => {
    const reload = holdReload();
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));

    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });
    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledTimes(2));
    await act(async () => { reload.reject(new Error(RELOAD_FAILURE)); });
    expect(screen.getByRole('status').textContent).toBe(`API 配置已保存。${RELOAD_FAILURE}`);

    fireEvent.click(screen.getByRole('button', { name: '刷新' }));

    await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
  });

  it('换了 token 重新进页面，提示清空', async () => {
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));
    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });
    expect(await statusText()).toBe('API 配置已保存。');

    act(() => { useSettingsStore.setState({ adminToken: 'another-token' }); });

    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
  });
});

describe('每一份配置带自己的指纹', () => {
  const withPrompts = (): DrawtoolsBundle => ({
    ...bundle(),
    prompts: {
      top_system: { content: 'top\n', hash: 'top-1', exists: true },
      output_format: { content: 'fmt\n', hash: 'fmt-1', exists: true },
    },
  });

  it('原文编辑器保存的是当前分区那一份的指纹', async () => {
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(rawEditor()).toHaveValue(SETTINGS_YAML));

    fireEvent.change(rawEditor(), { target: { value: EDITED_SETTINGS_YAML } });
    fireEvent.click(rawButton('保存当前文件'));

    await waitFor(() => expect(api.updateDrawtoolsSettingsRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateDrawtoolsSettingsRaw).mock.calls[0][2]).toBe('settings-1');
  });

  it('提示词分区按下划线键去整包里取指纹，保存走连字符那条路径', async () => {
    serve(withPrompts());
    vi.mocked(api.updateDrawtoolsPromptRaw).mockResolvedValue({ ok: true, hash: 'top-2' });
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(rawEditor()).toHaveValue(SETTINGS_YAML));

    pickSection('top-system');
    await waitFor(() => expect(rawEditor()).toHaveValue('top\n'));
    fireEvent.change(rawEditor(), { target: { value: 'top v2\n' } });
    fireEvent.click(rawButton('保存当前文件'));

    await waitFor(() => expect(api.updateDrawtoolsPromptRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateDrawtoolsPromptRaw).mock.calls[0])
      .toEqual(['admin-token', 'top-system', 'top v2\n', 'top-1']);
  });

  it('节点分区带的是节点文件的指纹', async () => {
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(rawEditor()).toHaveValue(SETTINGS_YAML));

    pickSection('node');
    await waitFor(() => expect(rawEditor()).toHaveValue('id: draw.novelai_planner\n'));
    fireEvent.change(rawEditor(), { target: { value: NODE_YAML } });
    fireEvent.click(rawButton('保存当前文件'));

    await waitFor(() => expect(api.updateNodeRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateNodeRaw).mock.calls[0][3]).toBe('planner-1');
  });

  it('结构化字段直写也带整包里那一份的指纹', async () => {
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));

    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });

    await waitFor(() => expect(api.updateDrawtoolsSettingsRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateDrawtoolsSettingsRaw).mock.calls[0][2]).toBe('settings-1');
  });

  it('角色标签库那一份记的是自己的指纹', async () => {
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(screen.getByText('初音未来')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    fireEvent.blur(screen.getByPlaceholderText('hatsune_miku'), { target: { value: 'hatsune_miku_v4' } });

    await waitFor(() => expect(api.updateDrawtoolsCharactersRaw).toHaveBeenCalledOnce());
    expect(vi.mocked(api.updateDrawtoolsCharactersRaw).mock.calls[0][2]).toBe('characters-1');
  });
});

describe('分析节点那一栏', () => {
  const settled = () => waitFor(() => expect(rawEditor()).toHaveValue(SETTINGS_YAML));

  it('节点文件还没建就按空文件编辑，保存把它建出来', async () => {
    vi.mocked(api.getNodeRaw).mockRejectedValue(new Error('404 Not Found'));
    render(<DrawtoolsSettingsPage />);
    await settled();

    pickSection('node');
    expect(rawEditor()).toHaveValue('');
    expect(rawEditor()).toBeEnabled();
    expect(screen.queryByRole('alert')).toBeNull();

    fireEvent.change(rawEditor(), { target: { value: NODE_YAML } });
    fireEvent.click(rawButton('保存当前文件'));

    await waitFor(() => expect(api.updateNodeRaw).toHaveBeenCalledOnce());
    // 读到 404 时指纹是空串：保存要求它仍然不存在，被别处抢先建了就报冲突。
    expect(vi.mocked(api.updateNodeRaw).mock.calls[0])
      .toEqual(['admin-token', 'draw.novelai_planner', NODE_YAML, '']);
  });

  it('别的读失败就锁住这一栏，说清为什么不让改', async () => {
    vi.mocked(api.getNodeRaw).mockRejectedValue(new Error(NODE_FAILURE));
    render(<DrawtoolsSettingsPage />);
    await settled();

    pickSection('node');

    expect(rawEditor()).toBeDisabled();
    expect(screen.getByRole('alert').textContent).toBe(NODE_LOCKED);

    // 绕开 disabled 硬塞一段草稿：两个按钮得靠锁本身关着，不能只靠「没改过」。
    fireEvent.change(rawEditor(), { target: { value: NODE_YAML } });
    expect(rawButton('保存当前文件')).toBeDisabled();
    expect(rawButton('丢弃')).toBeDisabled();
  });

  it('节点读不回来不连累别的栏', async () => {
    vi.mocked(api.getNodeRaw).mockRejectedValue(new Error(NODE_FAILURE));
    render(<DrawtoolsSettingsPage />);
    await settled();

    expect(rawEditor()).toBeEnabled();
    expect(screen.queryByRole('alert')).toBeNull();

    fireEvent.change(rawEditor(), { target: { value: EDITED_SETTINGS_YAML } });
    fireEvent.click(rawButton('保存当前文件'));

    await waitFor(() => expect(api.updateDrawtoolsSettingsRaw).toHaveBeenCalledOnce());
    expect(written(vi.mocked(api.updateDrawtoolsSettingsRaw))).toBe(EDITED_SETTINGS_YAML);
    expect(api.updateNodeRaw).not.toHaveBeenCalled();
  });

  it('刷新读到了就解锁，提示跟着消失', async () => {
    vi.mocked(api.getNodeRaw).mockRejectedValueOnce(new Error(NODE_FAILURE)).mockResolvedValue({ content: NODE_YAML, hash: 'planner-1' });
    render(<DrawtoolsSettingsPage />);
    await settled();
    pickSection('node');
    expect(rawEditor()).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '刷新' }));

    await waitFor(() => expect(rawEditor()).toBeEnabled());
    expect(rawEditor()).toHaveValue(NODE_YAML);
    expect(screen.queryByRole('alert')).toBeNull();

    fireEvent.change(rawEditor(), { target: { value: `${NODE_YAML}persistent: true\n` } });
    expect(rawButton('保存当前文件')).toBeEnabled();
  });
});

describe('接连重读只认最后发出的那次', () => {
  /** 首读压着不放，换 token 再读一次 —— 两次读谁先回来由测试摆布。 */
  const raceTwoLoads = () => {
    const first = deferred<DrawtoolsBundle>();
    const second = deferred<DrawtoolsBundle>();
    vi.mocked(api.getDrawtoolsBundle).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    return { first, second };
  };

  const startSecondLoad = async () => {
    act(() => { useSettingsStore.setState({ adminToken: 'another-token' }); });
    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledTimes(2));
  };

  it('慢回来的旧数据不覆盖新的，loading 也只由最新那次收掉', async () => {
    const { first, second } = raceTwoLoads();
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledOnce());
    await startSecondLoad();

    await act(async () => { first.resolve(bundle(STALE_SETTINGS_YAML)); });

    expect(rawEditor()).toHaveValue('');
    expect(screen.getByRole('button', { name: '正在刷新…' })).toBeDisabled();

    await act(async () => { second.resolve(bundle()); });

    expect(rawEditor()).toHaveValue(SETTINGS_YAML);
    expect(screen.getByRole('button', { name: '刷新' })).toBeEnabled();
  });

  it('慢回来的那次读失败，不摆出过时的原因', async () => {
    const { first, second } = raceTwoLoads();
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledOnce());
    await startSecondLoad();
    await act(async () => { second.resolve(bundle()); });

    await act(async () => { first.reject(new Error(RELOAD_FAILURE)); });

    expect(screen.queryByRole('status')).toBeNull();
    expect(rawEditor()).toHaveValue(SETTINGS_YAML);
    expect(screen.getByRole('button', { name: '刷新' })).toBeEnabled();
  });
});

describe('写出去之后换了 token', () => {
  const ROTATED = 'rotated-token';
  const WRITE_FAILURE = '503 写不进用户配置';

  const settled = () => waitFor(() => expect(rawEditor()).toHaveValue(SETTINGS_YAML));

  /** 换 token 自己就会重读一次，之后的读都是这一趟守卫漏掉的。 */
  const rotate = async () => {
    act(() => { useSettingsStore.setState({ adminToken: ROTATED }); });
    await waitFor(() => expect(api.getDrawtoolsBundle).toHaveBeenCalledTimes(2));
  };

  const loadTokens = () => vi.mocked(api.getDrawtoolsBundle).mock.calls.map(([token]) => token);

  const saveRawEditor = () => {
    fireEvent.change(rawEditor(), { target: { value: EDITED_SETTINGS_YAML } });
    fireEvent.click(rawButton('保存当前文件'));
  };

  it('保存当前文件写完才换 token，不提示也不拿旧 token 重读', async () => {
    const write = deferred<api.RawSaveResult>();
    vi.mocked(api.updateDrawtoolsSettingsRaw).mockReturnValueOnce(write.promise);
    render(<DrawtoolsSettingsPage />);
    await settled();

    saveRawEditor();
    await waitFor(() => expect(api.updateDrawtoolsSettingsRaw).toHaveBeenCalledOnce());
    await rotate();
    await act(async () => { write.resolve({ ok: true, hash: 'settings-2' }); });

    expect(loadTokens()).toEqual(['admin-token', ROTATED]);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('保存当前文件失败之前换了 token，不摆出这一趟的报错', async () => {
    const write = deferred<api.RawSaveResult>();
    vi.mocked(api.updateDrawtoolsSettingsRaw).mockReturnValueOnce(write.promise);
    render(<DrawtoolsSettingsPage />);
    await settled();

    saveRawEditor();
    await waitFor(() => expect(api.updateDrawtoolsSettingsRaw).toHaveBeenCalledOnce());
    await rotate();
    await act(async () => { write.reject(new Error(WRITE_FAILURE)); });

    expect(screen.queryByRole('status')).toBeNull();
  });

  it('初始化建完文件才换 token，不提示也不拿旧 token 重读', async () => {
    const init = deferred<{ ok: boolean; created: string[] }>();
    vi.mocked(api.initDrawtoolsConfigs).mockReturnValueOnce(init.promise);
    render(<DrawtoolsSettingsPage />);
    await settled();

    fireEvent.click(screen.getByRole('button', { name: '从 Example 初始化用户配置' }));
    await waitFor(() => expect(api.initDrawtoolsConfigs).toHaveBeenCalledOnce());
    await rotate();
    await act(async () => { init.resolve({ ok: true, created: ['settings.yaml'] }); });

    expect(loadTokens()).toEqual(['admin-token', ROTATED]);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('初始化失败之前换了 token，不摆出这一趟的报错', async () => {
    const init = deferred<{ ok: boolean; created: string[] }>();
    vi.mocked(api.initDrawtoolsConfigs).mockReturnValueOnce(init.promise);
    render(<DrawtoolsSettingsPage />);
    await settled();

    fireEvent.click(screen.getByRole('button', { name: '从 Example 初始化用户配置' }));
    await waitFor(() => expect(api.initDrawtoolsConfigs).toHaveBeenCalledOnce());
    await rotate();
    await act(async () => { init.reject(new Error(WRITE_FAILURE)); });

    expect(screen.queryByRole('status')).toBeNull();
  });

  it('结构化字段写完才换 token，不提示也不拿旧 token 重读', async () => {
    const write = deferred<api.RawSaveResult>();
    vi.mocked(api.updateDrawtoolsSettingsRaw).mockReturnValueOnce(write.promise);
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));

    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });
    await waitFor(() => expect(api.updateDrawtoolsSettingsRaw).toHaveBeenCalledOnce());
    await rotate();
    await act(async () => { write.resolve({ ok: true, hash: 'settings-2' }); });

    expect(loadTokens()).toEqual(['admin-token', ROTATED]);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('结构化字段写失败之前换了 token，不摆出这一趟的报错', async () => {
    const write = deferred<api.RawSaveResult>();
    vi.mocked(api.updateDrawtoolsSettingsRaw).mockReturnValueOnce(write.promise);
    render(<DrawtoolsSettingsPage />);
    await waitFor(() => expect(baseUrlBox()).toHaveValue('https://image.novelai.net'));

    fireEvent.blur(baseUrlBox(), { target: { value: 'https://relay.example' } });
    await waitFor(() => expect(api.updateDrawtoolsSettingsRaw).toHaveBeenCalledOnce());
    await rotate();
    await act(async () => { write.reject(new Error(WRITE_FAILURE)); });

    expect(screen.queryByRole('status')).toBeNull();
  });

  it('清理跑完才换 token，删了几个文件不报到新会话', async () => {
    const cleanup = deferred<unknown>();
    vi.mocked(api.cleanupDrawtoolsAttachments).mockReturnValueOnce(cleanup.promise);
    render(<DrawtoolsSettingsPage />);
    await settled();

    fireEvent.click(screen.getByRole('button', { name: '立即清理' }));
    await waitFor(() => expect(api.cleanupDrawtoolsAttachments).toHaveBeenCalledOnce());
    await rotate();
    await act(async () => {
      cleanup.resolve({ deleted_count: 3, deleted_mb: 12, remaining_count: 8, remaining_mb: 40 });
    });

    expect(screen.queryByRole('status')).toBeNull();
  });

  it('清理失败之前换了 token，不摆出这一趟的报错', async () => {
    const cleanup = deferred<unknown>();
    vi.mocked(api.cleanupDrawtoolsAttachments).mockReturnValueOnce(cleanup.promise);
    render(<DrawtoolsSettingsPage />);
    await settled();

    fireEvent.click(screen.getByRole('button', { name: '立即清理' }));
    await waitFor(() => expect(api.cleanupDrawtoolsAttachments).toHaveBeenCalledOnce());
    await rotate();
    await act(async () => { cleanup.reject(new Error(WRITE_FAILURE)); });

    expect(screen.queryByRole('status')).toBeNull();
  });
});
