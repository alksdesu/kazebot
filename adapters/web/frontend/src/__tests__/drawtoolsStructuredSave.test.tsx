// 绘图页的结构化字段写回：只重写改了的键，运营者手写的注释留着；原文坏了一个字节都不写。
// 保存的提示要活过紧跟着的那次重载，只有「刷新」清得掉。
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  settings: { content: settings, exists: true, path: 'config/drawtools/settings.yaml' },
  character_tags: { content: characters, exists: true, path: 'config/drawtools/character_tags.yaml' },
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

/** 没有消息时整个 StatusText 不渲染，只能等它出现。 */
const statusText = async (): Promise<string> => (await screen.findByRole('status')).textContent || '';

const baseUrlBox = () => screen.getByPlaceholderText('https://image.novelai.net');

const written = (writer: { mock: { calls: unknown[][] } }): string => String(writer.mock.calls[0][1]);

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true });
  serve(bundle());
  vi.mocked(api.getNodeRaw).mockResolvedValue('id: draw.novelai_planner\n');
  vi.mocked(api.initDrawtoolsConfigs).mockResolvedValue({ ok: true, created: [] });
  vi.mocked(api.updateDrawtoolsSettingsRaw).mockResolvedValue({ ok: true });
  vi.mocked(api.updateDrawtoolsCharactersRaw).mockResolvedValue({ ok: true });
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
