import { useEffect, useMemo, useRef, useState, type TextareaHTMLAttributes } from 'react';
import yaml from 'js-yaml';

import {
  cleanupDrawtoolsAttachments,
  getDrawtoolsBundle,
  getNodeRaw,
  initDrawtoolsConfigs,
  updateDrawtoolsCharactersRaw,
  updateDrawtoolsPromptRaw,
  updateDrawtoolsSettingsRaw,
  updateNodeRaw,
  type DrawtoolsBundle,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';
import { AuthRequired, Card, FieldLabel, PageHeader, PageShell, SelectInput, StatusText, TextInput } from './settingsPagePrimitives';

const presetTextAreaClass = 'w-full resize-y overflow-hidden border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-sm leading-5 text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';

function autoResizeTextArea(target: HTMLTextAreaElement) {
  target.style.height = 'auto';
  target.style.height = `${Math.max(target.scrollHeight, 72)}px`;
}

function AutoResizeTextarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const ref = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (ref.current) autoResizeTextArea(ref.current);
  }, [props.defaultValue, props.value]);

  return (
    <textarea
      {...props}
      className={`${presetTextAreaClass} ${props.className || ''}`}
      onInput={(event) => {
        autoResizeTextArea(event.currentTarget);
        props.onInput?.(event);
      }}
      ref={ref}
    />
  );
}

function makePresetId(name: string, presets: any[]): string {
  const base = (name || 'preset')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '_')
    .replace(/^_+|_+$/g, '') || 'preset';
  const used = new Set(presets.map((preset: any) => String(preset?.id || '')));
  if (!used.has(base)) return base;
  let index = 2;
  while (used.has(`${base}_${index}`)) index += 1;
  return `${base}_${index}`;
}

type SectionKey = 'settings' | 'characters' | 'node' | 'top-system' | 'output-format' | 'tag-guide';

const sectionLabels: Record<SectionKey, string> = {
  settings: '绘图设置 / 预设',
  characters: '角色标签库',
  node: '绘图分析节点 YAML',
  'top-system': 'Top System',
  'output-format': '输出格式',
  'tag-guide': 'Tag 指南',
};

const NAI_MODELS = [
  { value: 'nai-diffusion-4-5-full', label: 'nai-diffusion-4-5-full（V4.5 完整版，默认）' },
  { value: 'nai-diffusion-4-5-curated', label: 'nai-diffusion-4-5-curated（V4.5 策展版 / SFW）' },
  { value: 'nai-diffusion-4-full', label: 'nai-diffusion-4-full（V4 完整版）' },
  { value: 'nai-diffusion-4-curated', label: 'nai-diffusion-4-curated（V4 策展版）' },
  { value: 'nai-diffusion-3', label: 'nai-diffusion-3（V3 旧版）' },
];

const NAI_SAMPLERS = [
  'k_euler',
  'k_euler_ancestral',
  'k_dpmpp_2s_ancestral',
  'k_dpmpp_2m_sde',
  'k_dpmpp_2m',
  'k_dpmpp_sde',
];

const NAI_SCHEDULERS = ['karras', 'exponential', 'polyexponential'];

type CharacterListItem = { character: any; index: number };

const CHARACTER_TYPES = [
  { value: 'girl', label: 'girl / 女性' },
  { value: 'boy', label: 'boy / 男性' },
  { value: 'animal', label: 'animal / 动物' },
  { value: 'creature', label: 'creature / 生物' },
  { value: 'object', label: 'object / 物品' },
  { value: 'custom', label: 'custom / 自定义' },
];

function normalizeCharacterOutfits(outfits: any): { name: string; tags: string }[] {
  return (Array.isArray(outfits) ? outfits : [])
    .map((outfit: any) => ({
      name: String(outfit?.name || '').trim(),
      tags: String(outfit?.tags || '').trim(),
    }))
    .filter((outfit) => outfit.name || outfit.tags);
}

function characterSearchText(character: any): string {
  return [
    character?.name,
    ...(Array.isArray(character?.aliases) ? character.aliases : []),
    character?.danbooru,
    character?.type,
    character?.appearance,
    character?.negative_tags,
    character?.negativeTags,
    ...normalizeCharacterOutfits(character?.outfits).flatMap((outfit) => [outfit.name, outfit.tags]),
  ].filter(Boolean).join(' ').toLowerCase();
}

function parseYamlObject(text: string): any {
  try {
    const data = yaml.load(text);
    return data && typeof data === 'object' ? data : {};
  } catch {
    return {};
  }
}

function stringifyYaml(data: unknown): string {
  return yaml.dump(data, { lineWidth: 120, noRefs: true });
}

function getSectionContent(bundle: DrawtoolsBundle | null, key: SectionKey): string {
  if (!bundle) return '';
  if (key === 'settings') return bundle.settings?.content || '';
  if (key === 'characters') return bundle.character_tags?.content || '';
  if (key === 'node') return '';
  return bundle.prompts?.[key.replace('-', '_')]?.content || '';
}

function fileMeta(bundle: DrawtoolsBundle | null, key: SectionKey): string {
  if (!bundle) return '';
  if (key === 'node') return 'config/nodes/draw.novelai_planner.yaml';
  const item = key === 'settings'
    ? bundle.settings
    : key === 'characters'
      ? bundle.character_tags
      : bundle.prompts?.[key.replace('-', '_')];
  if (!item) return '';
  return item.exists ? `用户配置：${item.path}` : `正在显示模板：${item.example_path}`;
}

export const DrawtoolsSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [bundle, setBundle] = useState<DrawtoolsBundle | null>(null);
  const [section, setSection] = useState<SectionKey>('settings');
  const [editor, setEditor] = useState('');
  const [nodeRaw, setNodeRaw] = useState('');
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);
  const [presetsExpanded, setPresetsExpanded] = useState(false);
  const [characterQuery, setCharacterQuery] = useState('');
  const [editingCharacterIndex, setEditingCharacterIndex] = useState<number | null>(null);

  const settingsObj = useMemo(() => parseYamlObject(bundle?.settings?.content || ''), [bundle]);
  const presets = Array.isArray(settingsObj?.params?.presets) ? settingsObj.params.presets : [];
  const selectedPresetId = String(settingsObj?.params?.selected_preset_id || '');
  const api = settingsObj?.api || {};
  const generation = settingsObj?.generation || {};
  const storage = settingsObj?.storage || {};
  const characterObj = useMemo(() => parseYamlObject(bundle?.character_tags?.content || ''), [bundle]);
  const characters = Array.isArray(characterObj?.characters) ? characterObj.characters : [];
  const characterItems: CharacterListItem[] = characters.map((character: any, index: number) => ({ character, index }));
  const normalizedCharacterQuery = characterQuery.trim().toLowerCase();
  const filteredCharacters: CharacterListItem[] = normalizedCharacterQuery
    ? characterItems.filter((item: CharacterListItem) => characterSearchText(item.character).includes(normalizedCharacterQuery))
    : characterItems;
  const effectiveSelectedPresetId = selectedPresetId || String(presets[0]?.id || '');
  const selectedPreset = presets.find((preset: any) => String(preset?.id || '') === effectiveSelectedPresetId) || presets[0] || {};

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    try {
      const [data, nodeYaml] = await Promise.all([
        getDrawtoolsBundle(adminToken),
        getNodeRaw(adminToken, 'draw.novelai_planner').catch(() => ''),
      ]);
      setBundle(data);
      setNodeRaw(nodeYaml);
      setEditor(section === 'node' ? nodeYaml : getSectionContent(data, section));
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '加载绘图配置失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [adminToken, isAuthenticated]);

  useEffect(() => {
    setEditor(section === 'node' ? nodeRaw : getSectionContent(bundle, section));
  }, [bundle, nodeRaw, section]);

  const saveCurrent = async () => {
    if (!adminToken) return;
    try {
      if (section === 'settings') await updateDrawtoolsSettingsRaw(adminToken, editor);
      else if (section === 'characters') await updateDrawtoolsCharactersRaw(adminToken, editor);
      else if (section === 'node') await updateNodeRaw(adminToken, 'draw.novelai_planner', editor);
      else await updateDrawtoolsPromptRaw(adminToken, section, editor);
      setMessage('已保存。用户配置文件会写入非 example 文件，后续更新不会覆盖。');
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存失败');
    }
  };

  const initConfigs = async () => {
    if (!adminToken) return;
    try {
      const result = await initDrawtoolsConfigs(adminToken);
      setMessage(result.created.length ? `已创建：${result.created.join('、')}` : '用户配置已存在，无需创建。');
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '初始化失败');
    }
  };

  const saveSettingsObject = async (next: any, okText: string) => {
    if (!adminToken) return;
    try {
      await updateDrawtoolsSettingsRaw(adminToken, stringifyYaml(next));
      setMessage(okText);
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存设置失败');
    }
  };

  const saveCharacterObject = async (next: any, okText: string) => {
    if (!adminToken) return;
    try {
      await updateDrawtoolsCharactersRaw(adminToken, stringifyYaml(next));
      setMessage(okText);
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存角色标签库失败');
    }
  };

  const updateApiField = (key: string, value: string) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    next.api = { ...(next.api || {}), [key]: value };
    void saveSettingsObject(next, 'API 配置已保存。');
  };

  const updateGenerationField = (key: string, value: string) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    const numeric = Number(value);
    next.generation = { ...(next.generation || {}), [key]: Number.isFinite(numeric) ? numeric : value };
    void saveSettingsObject(next, '生成队列/重试配置已保存。');
  };

  const updateStorageField = (key: string, value: string | boolean) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    const numeric = typeof value === 'string' ? Number(value) : NaN;
    next.storage = { ...(next.storage || {}), [key]: typeof value === 'boolean' ? value : (Number.isFinite(numeric) ? numeric : value) };
    void saveSettingsObject(next, '附件清理配置已保存。');
  };

  const runCleanup = async () => {
    if (!adminToken) return;
    try {
      const result = await cleanupDrawtoolsAttachments(adminToken);
      setMessage(`清理完成：删除 ${result.deleted_count ?? 0} 个文件，释放 ${result.deleted_mb ?? 0} MB；剩余 ${result.remaining_count ?? 0} 个文件 / ${result.remaining_mb ?? 0} MB`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '清理失败');
    }
  };

  const switchPreset = (presetId: string) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    next.params = { ...(next.params || {}), selected_preset_id: presetId };
    void saveSettingsObject(next, `已切换默认预设：${presetId}`);
  };

  const addPreset = () => {
    const displayName = window.prompt('新预设名称：', '新绘图预设');
    if (!displayName) return;
    const next = parseYamlObject(bundle?.settings?.content || '');
    const list = Array.isArray(next?.params?.presets) ? [...next.params.presets] : [];
    const id = makePresetId(displayName, list);
    const sourceParams = selectedPreset?.params && typeof selectedPreset.params === 'object' ? selectedPreset.params : {};
    const created = {
      id,
      name: displayName.trim() || id,
      aliases: [],
      positive_prefix: String(selectedPreset?.positive_prefix || ''),
      negative_prefix: String(selectedPreset?.negative_prefix || ''),
      params: {
        model: sourceParams.model || 'nai-diffusion-4-5-full',
        sampler: sourceParams.sampler || 'k_euler_ancestral',
        scheduler: sourceParams.scheduler || 'karras',
        steps: sourceParams.steps ?? 28,
        scale: sourceParams.scale ?? 6,
        cfg_rescale: sourceParams.cfg_rescale ?? 0,
        ucPreset: sourceParams.ucPreset ?? 0,
        qualityToggle: sourceParams.qualityToggle ?? true,
        autoSmea: sourceParams.autoSmea ?? false,
        variety_boost: sourceParams.variety_boost ?? false,
      },
    };
    list.push(created);
    next.params = { ...(next.params || {}), presets: list, selected_preset_id: id };
    void saveSettingsObject(next, `已新增预设：${created.name}`);
  };

  const renameSelectedPreset = () => {
    if (!selectedPreset?.id) { setMessage('请先选择一个预设'); return; }
    const name = window.prompt('预设新名称：', String(selectedPreset.name || selectedPreset.id));
    if (!name) return;
    updateSelectedPresetField('name', name.trim() || String(selectedPreset.id));
  };

  const deleteSelectedPreset = () => {
    if (!selectedPreset?.id) { setMessage('请先选择一个预设'); return; }
    if (!window.confirm(`确定删除预设「${selectedPreset.name || selectedPreset.id}」？`)) return;
    const next = parseYamlObject(bundle?.settings?.content || '');
    const list = Array.isArray(next?.params?.presets) ? next.params.presets : [];
    const filtered = list.filter((preset: any) => String(preset?.id || '') !== String(selectedPreset.id));
    const nextSelectedId = String(filtered[0]?.id || '');
    next.params = { ...(next.params || {}), presets: filtered, selected_preset_id: nextSelectedId };
    void saveSettingsObject(next, `已删除预设：${selectedPreset.name || selectedPreset.id}`);
  };

  const updateSelectedPresetParam = (key: string, value: string | boolean) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    const list = Array.isArray(next?.params?.presets) ? next.params.presets : [];
    const idx = list.findIndex((preset: any) => String(preset?.id || '') === effectiveSelectedPresetId);
    if (idx < 0) { setMessage('请先选择一个预设'); return; }
    const numeric = typeof value === 'string' ? Number(value) : NaN;
    list[idx] = {
      ...list[idx],
      params: {
        ...(list[idx].params || {}),
        [key]: typeof value === 'boolean' ? value : (Number.isFinite(numeric) && value !== '' ? numeric : value),
      },
    };
    next.params = { ...(next.params || {}), presets: list };
    void saveSettingsObject(next, `已保存预设参数：${key}`);
  };

  const updateSelectedPresetField = (key: string, value: string | string[]) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    const list = Array.isArray(next?.params?.presets) ? next.params.presets : [];
    const idx = list.findIndex((preset: any) => String(preset?.id || '') === effectiveSelectedPresetId);
    if (idx < 0) { setMessage('请先选择一个预设'); return; }
    list[idx] = { ...list[idx], [key]: value };
    next.params = { ...(next.params || {}), presets: list };
    void saveSettingsObject(next, `已保存预设字段：${key}`);
  };

  const makeCharacterObject = () => {
    const next = parseYamlObject(bundle?.character_tags?.content || '');
    if (!Array.isArray(next.characters)) next.characters = [];
    return next;
  };

  const addCharacter = () => {
    const name = window.prompt('新角色名称：', '新角色');
    if (!name) return;
    const next = makeCharacterObject();
    next.characters.push({
      name: name.trim() || '新角色',
      aliases: [],
      danbooru: '',
      type: 'girl',
      appearance: '',
      outfits: [],
    });
    setEditingCharacterIndex(next.characters.length - 1);
    void saveCharacterObject(next, `已新增角色：${name.trim() || '新角色'}`);
  };

  const renameCharacter = (index: number) => {
    const current = characters[index];
    if (!current) return;
    const name = window.prompt('角色新名称：', String(current.name || ''));
    if (!name) return;
    const next = makeCharacterObject();
    if (!next.characters[index]) return;
    next.characters[index] = { ...next.characters[index], name: name.trim() || String(current.name || '新角色') };
    void saveCharacterObject(next, `已重命名角色：${name.trim() || current.name}`);
  };

  const deleteCharacter = (index: number) => {
    const current = characters[index];
    if (!current) return;
    if (!window.confirm(`确定删除角色「${current.name || `#${index + 1}`}」？`)) return;
    const next = makeCharacterObject();
    next.characters.splice(index, 1);
    setEditingCharacterIndex(null);
    void saveCharacterObject(next, `已删除角色：${current.name || `#${index + 1}`}`);
  };

  const updateCharacterField = (index: number, key: string, value: string | string[]) => {
    const next = makeCharacterObject();
    if (!next.characters[index]) return;
    next.characters[index] = { ...next.characters[index], [key]: value };
    void saveCharacterObject(next, `已保存角色字段：${key}`);
  };

  const addCharacterOutfit = (index: number) => {
    const next = makeCharacterObject();
    if (!next.characters[index]) return;
    const outfits = normalizeCharacterOutfits(next.characters[index].outfits);
    outfits.push({ name: '新服装', tags: '' });
    next.characters[index] = { ...next.characters[index], outfits };
    setEditingCharacterIndex(index);
    void saveCharacterObject(next, '已添加角色服装');
  };

  const deleteCharacterOutfit = (characterIndex: number, outfitIndex: number) => {
    const next = makeCharacterObject();
    if (!next.characters[characterIndex]) return;
    const outfits = normalizeCharacterOutfits(next.characters[characterIndex].outfits);
    outfits.splice(outfitIndex, 1);
    next.characters[characterIndex] = { ...next.characters[characterIndex], outfits };
    void saveCharacterObject(next, '已删除角色服装');
  };

  const updateCharacterOutfit = (characterIndex: number, outfitIndex: number, key: 'name' | 'tags', value: string) => {
    const next = makeCharacterObject();
    if (!next.characters[characterIndex]) return;
    const outfits = normalizeCharacterOutfits(next.characters[characterIndex].outfits);
    if (!outfits[outfitIndex]) return;
    outfits[outfitIndex] = { ...outfits[outfitIndex], [key]: value };
    next.characters[characterIndex] = { ...next.characters[characterIndex], outfits };
    void saveCharacterObject(next, `已保存服装${key === 'name' ? '名称' : '标签'}`);
  };

  return (
    <PageShell>
      <PageHeader
        description="管理 NovelAI 绘图配置、画师串预设、角色标签库和提示词模板。默认只维护 .example，点击初始化后才创建用户配置文件。"
        title="NovelAI 绘图"
      />
      {!isAuthenticated ? <AuthRequired /> : (
        <>
          <Card title="运行期配置" description="用户配置写入 settings.yaml / character_tags.yaml / prompt .md；仓库更新只维护 .example。">
            <div className="flex flex-wrap gap-2">
              <Button disabled={loading} onClick={load}>{loading ? '加载中...' : '刷新'}</Button>
              <Button onClick={initConfigs} variant="primary">从 Example 初始化用户配置</Button>
            </div>
            <StatusText message={message} />
          </Card>

          <Card title="API 配置" description="API Key 推荐使用环境变量 NOVELAI_API_KEY；如必须写入文件，可在下方保存。">
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <FieldLabel>NovelAI / 兼容站点 Base URL</FieldLabel>
                <TextInput defaultValue={String(api.base_url || '')} onBlur={(event) => updateApiField('base_url', event.currentTarget.value)} placeholder="https://image.novelai.net" />
              </div>
              <div>
                <FieldLabel>API Key 环境变量名</FieldLabel>
                <TextInput defaultValue={String(api.api_key_env || 'NOVELAI_API_KEY')} onBlur={(event) => updateApiField('api_key_env', event.currentTarget.value)} />
              </div>
              <div className="md:col-span-2">
                <FieldLabel>API Key（可选，不推荐提交到仓库）</FieldLabel>
                <TextInput defaultValue={String(api.api_key || '')} onBlur={(event) => updateApiField('api_key', event.currentTarget.value)} placeholder="留空则使用环境变量" type="password" />
              </div>
            </div>
          </Card>

          <Card title="生成队列 / 重试" description="NovelAI 请求会通过全局队列串行执行；429/5xx/连接失败会自动重试。">
            <div className="grid gap-3 md:grid-cols-3">
              <div>
                <FieldLabel>生成间隔（秒）</FieldLabel>
                <TextInput defaultValue={String(generation.request_delay_sec ?? 0)} onBlur={(event) => updateGenerationField('request_delay_sec', event.currentTarget.value)} type="number" />
              </div>
              <div>
                <FieldLabel>重试等待（秒）</FieldLabel>
                <TextInput defaultValue={String(generation.retry_wait_sec ?? 3)} onBlur={(event) => updateGenerationField('retry_wait_sec', event.currentTarget.value)} type="number" />
              </div>
              <div>
                <FieldLabel>重试次数</FieldLabel>
                <TextInput defaultValue={String(generation.retry_max_attempts ?? 5)} onBlur={(event) => updateGenerationField('retry_max_attempts', event.currentTarget.value)} type="number" />
              </div>
            </div>
          </Card>

          <Card title="附件清理" description="自动清理 data/attachments/novelai：先删除过期文件，若仍超过容量上限再删除最旧文件。">
            <div className="grid gap-3 md:grid-cols-4">
              <label className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                <input checked={Boolean(storage.cleanup_enabled ?? true)} onChange={(event) => updateStorageField('cleanup_enabled', event.currentTarget.checked)} type="checkbox" />
                启用自动清理
              </label>
              <div>
                <FieldLabel>保留天数</FieldLabel>
                <TextInput defaultValue={String(storage.retention_days ?? 7)} onBlur={(event) => updateStorageField('retention_days', event.currentTarget.value)} type="number" />
              </div>
              <div>
                <FieldLabel>最大容量 MB</FieldLabel>
                <TextInput defaultValue={String(storage.max_total_mb ?? 2048)} onBlur={(event) => updateStorageField('max_total_mb', event.currentTarget.value)} type="number" />
              </div>
              <div>
                <FieldLabel>清理检查间隔 秒</FieldLabel>
                <TextInput defaultValue={String(storage.cleanup_interval_sec ?? 3600)} onBlur={(event) => updateStorageField('cleanup_interval_sec', event.currentTarget.value)} type="number" />
              </div>
            </div>
            <Button className="mt-3" onClick={runCleanup}>立即清理</Button>
          </Card>

          <Card title="画师串 / 预设" description="绘图节点只看见预设名称，具体参数由工具层自动拼接。">
            <div className="mb-3 max-w-xs">
              <FieldLabel>当前默认预设</FieldLabel>
              <SelectInput onChange={(event) => switchPreset(event.currentTarget.value)} value={effectiveSelectedPresetId}>
                {presets.map((preset: any) => <option key={preset.id} value={preset.id}>{preset.name || preset.id}</option>)}
              </SelectInput>
            </div>
            <div className="mb-3 flex flex-wrap gap-2">
              <Button onClick={addPreset} variant="primary">+ 新增预设</Button>
              <Button disabled={!selectedPreset?.id} onClick={renameSelectedPreset}>重命名当前预设</Button>
              <Button disabled={!selectedPreset?.id} onClick={deleteSelectedPreset} variant="danger">删除当前预设</Button>
            </div>
            {selectedPreset?.id && (
              <div className="mb-4 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3">
                <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
                  <h3 className="font-mono text-xs font-semibold">当前预设快捷编辑：{selectedPreset.name || selectedPreset.id}</h3>
                  <span className="font-mono text-[0.65rem] text-[var(--duties-tertiary)]">id: {selectedPreset.id}</span>
                </div>
                <div className="grid gap-3 md:grid-cols-3">
                  <div>
                    <FieldLabel>预设名称</FieldLabel>
                    <TextInput key={`preset-name-${selectedPreset.id}`} defaultValue={String(selectedPreset.name || '')} onBlur={(event) => updateSelectedPresetField('name', event.currentTarget.value)} placeholder="显示给绘图节点和用户看的名称" />
                  </div>
                  <div>
                    <FieldLabel>别名（逗号分隔）</FieldLabel>
                    <TextInput key={`preset-aliases-${selectedPreset.id}`} defaultValue={Array.isArray(selectedPreset.aliases) ? selectedPreset.aliases.join(', ') : ''} onBlur={(event) => updateSelectedPresetField('aliases', event.currentTarget.value.split(',').map((item) => item.trim()).filter(Boolean))} placeholder="可选：中文名、短名" />
                  </div>
                  <div>
                    <FieldLabel>绘图模型</FieldLabel>
                    <SelectInput onChange={(event) => updateSelectedPresetParam('model', event.currentTarget.value)} value={String(selectedPreset.params?.model || 'nai-diffusion-4-5-full')}>
                      {NAI_MODELS.map((model) => <option key={model.value} value={model.value}>{model.label}</option>)}
                    </SelectInput>
                  </div>
                  <div>
                    <FieldLabel>采样器</FieldLabel>
                    <SelectInput onChange={(event) => updateSelectedPresetParam('sampler', event.currentTarget.value)} value={String(selectedPreset.params?.sampler || 'k_euler_ancestral')}>
                      {NAI_SAMPLERS.map((sampler) => <option key={sampler} value={sampler}>{sampler}</option>)}
                    </SelectInput>
                  </div>
                  <div>
                    <FieldLabel>噪声调度</FieldLabel>
                    <SelectInput onChange={(event) => updateSelectedPresetParam('scheduler', event.currentTarget.value)} value={String(selectedPreset.params?.scheduler || 'karras')}>
                      {NAI_SCHEDULERS.map((scheduler) => <option key={scheduler} value={scheduler}>{scheduler}</option>)}
                    </SelectInput>
                  </div>
                  <div>
                    <FieldLabel>Steps</FieldLabel>
                    <TextInput key={`steps-${selectedPreset.id}`} defaultValue={String(selectedPreset.params?.steps ?? 28)} onBlur={(event) => updateSelectedPresetParam('steps', event.currentTarget.value)} type="number" />
                  </div>
                  <div>
                    <FieldLabel>CFG / Scale</FieldLabel>
                    <TextInput key={`scale-${selectedPreset.id}`} defaultValue={String(selectedPreset.params?.scale ?? 6)} onBlur={(event) => updateSelectedPresetParam('scale', event.currentTarget.value)} type="number" />
                  </div>
                  <div>
                    <FieldLabel>负面预设</FieldLabel>
                    <SelectInput onChange={(event) => updateSelectedPresetParam('ucPreset', event.currentTarget.value)} value={String(selectedPreset.params?.ucPreset ?? 0)}>
                      <option value="0">Heavy</option>
                      <option value="1">Light</option>
                      <option value="2">Human Focus</option>
                      <option value="3">None</option>
                    </SelectInput>
                  </div>
                  <label className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                    <input checked={Boolean(selectedPreset.params?.qualityToggle ?? true)} onChange={(event) => updateSelectedPresetParam('qualityToggle', event.currentTarget.checked)} type="checkbox" />
                    质量增强
                  </label>
                  <label className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                    <input checked={Boolean(selectedPreset.params?.autoSmea ?? false)} onChange={(event) => updateSelectedPresetParam('autoSmea', event.currentTarget.checked)} type="checkbox" />
                    自动 SMEA
                  </label>
                  <label className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                    <input checked={Boolean(selectedPreset.params?.variety_boost ?? false)} onChange={(event) => updateSelectedPresetParam('variety_boost', event.currentTarget.checked)} type="checkbox" />
                    多样性增强 (V4.5)
                  </label>
                  <div>
                    <FieldLabel>CFG 重缩放</FieldLabel>
                    <TextInput key={`cfg-rescale-${selectedPreset.id}`} defaultValue={String(selectedPreset.params?.cfg_rescale ?? 0)} onBlur={(event) => updateSelectedPresetParam('cfg_rescale', event.currentTarget.value)} step="0.05" type="number" />
                  </div>
                  <div className="md:col-span-3">
                    <FieldLabel>正面前缀</FieldLabel>
                    <AutoResizeTextarea
                      defaultValue={String(selectedPreset.positive_prefix || '')}
                      key={`positive-prefix-${selectedPreset.id}`}
                      onBlur={(event) => updateSelectedPresetField('positive_prefix', event.currentTarget.value)}
                      placeholder="会自动拼接到正面提示词前方；支持多行编辑"
                      rows={3}
                    />
                  </div>
                  <div className="md:col-span-3">
                    <FieldLabel>负面前缀</FieldLabel>
                    <AutoResizeTextarea
                      defaultValue={String(selectedPreset.negative_prefix || '')}
                      key={`negative-prefix-${selectedPreset.id}`}
                      onBlur={(event) => updateSelectedPresetField('negative_prefix', event.currentTarget.value)}
                      placeholder="会自动拼接到负面提示词前方；支持多行编辑"
                      rows={3}
                    />
                  </div>
                </div>
              </div>
            )}
            <div>
              <button
                className="mb-2 flex w-full items-center justify-between border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 text-left font-mono text-xs font-semibold text-[var(--duties-text)] hover:border-[var(--duties-text)]"
                onClick={() => setPresetsExpanded((value) => !value)}
                type="button"
              >
                <span>预设列表（{presets.length} 个）</span>
                <span>{presetsExpanded ? '收起 ▲' : '展开 ▼'}</span>
              </button>
              {presetsExpanded && (
                <div className="max-h-80 space-y-2 overflow-y-auto pr-1">
                  {presets.map((preset: any) => (
                    <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3" key={preset.id}>
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="font-mono text-xs font-semibold">{preset.name || preset.id} {String(preset.id) === effectiveSelectedPresetId ? '· 当前默认' : ''}</div>
                        <Button onClick={() => switchPreset(String(preset.id))}>设为默认/编辑</Button>
                      </div>
                      <p className="mt-1 text-xs text-[var(--duties-secondary)]">id: {preset.id} · model: {preset.params?.model} · sampler: {preset.params?.sampler} · scheduler: {preset.params?.scheduler} · CFG: {preset.params?.scale} · steps: {preset.params?.steps}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Card>

          <Card title="角色标签库" description="管理绘图分析节点可参考的固定角色外貌、别名、Danbooru tag 和服装标签。保存后写入 character_tags.yaml。">
            <div className="mb-3 grid gap-2 md:grid-cols-[1fr_auto_auto]">
              <TextInput onChange={(event) => setCharacterQuery(event.currentTarget.value)} placeholder="搜索角色名称、别名、Danbooru、外貌、服装..." value={characterQuery} />
              <Button onClick={() => setCharacterQuery('')}>清空搜索</Button>
              <Button onClick={addCharacter} variant="primary">+ 新增角色</Button>
            </div>
            <div className="mb-3 grid gap-2 text-xs text-[var(--duties-secondary)] md:grid-cols-4">
              <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">总角色：{characters.length}</div>
              <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">搜索结果：{filteredCharacters.length}</div>
              <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">已填外貌：{characters.filter((character: any) => String(character?.appearance || '').trim()).length}</div>
              <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2">含服装：{characters.filter((character: any) => normalizeCharacterOutfits(character?.outfits).length).length}</div>
            </div>
            {!characters.length && <p className="text-sm text-[var(--duties-secondary)]">暂无角色配置，点击“新增角色”开始建立标签库。</p>}
            {!!characters.length && !filteredCharacters.length && <p className="text-sm text-[var(--duties-secondary)]">没有匹配的角色，试试调整搜索关键词。</p>}
            <div className="space-y-3">
              {filteredCharacters.map(({ character, index }: CharacterListItem) => {
                const isEditing = editingCharacterIndex === index;
                const outfits = normalizeCharacterOutfits(character?.outfits);
                const aliases = Array.isArray(character?.aliases) ? character.aliases : [];
                return (
                  <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3" key={`${character?.name || 'character'}-${index}`}>
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div>
                        <div className="font-mono text-sm font-semibold">{character?.name || `未命名角色 ${index + 1}`}</div>
                        <p className="mt-1 text-xs text-[var(--duties-secondary)]">
                          type: {character?.type || 'girl'} · aliases: {aliases.length || 0} · danbooru: {character?.danbooru || '—'} · outfits: {outfits.length}
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button onClick={() => setEditingCharacterIndex(isEditing ? null : index)}>{isEditing ? '收起编辑' : '编辑'}</Button>
                        <Button onClick={() => renameCharacter(index)}>重命名</Button>
                        <Button onClick={() => deleteCharacter(index)} variant="danger">删除</Button>
                      </div>
                    </div>
                    {!isEditing && (
                      <div className="mt-3 space-y-2 text-xs text-[var(--duties-secondary)]">
                        {character?.appearance ? <p><span className="font-semibold text-[var(--duties-text)]">外貌：</span>{character.appearance}</p> : <p>⚠️ 尚未填写外貌标签</p>}
                        {!!outfits.length && <p><span className="font-semibold text-[var(--duties-text)]">服装：</span>{outfits.map((outfit) => outfit.name || '未命名服装').join('、')}</p>}
                      </div>
                    )}
                    {isEditing && (
                      <div className="mt-4 grid gap-3 md:grid-cols-2">
                        <div>
                          <FieldLabel>角色名称</FieldLabel>
                          <TextInput key={`char-name-${index}`} defaultValue={String(character?.name || '')} onBlur={(event) => updateCharacterField(index, 'name', event.currentTarget.value)} />
                        </div>
                        <div>
                          <FieldLabel>类型</FieldLabel>
                          <SelectInput onChange={(event) => updateCharacterField(index, 'type', event.currentTarget.value)} value={String(character?.type || 'girl')}>
                            {CHARACTER_TYPES.map((type) => <option key={type.value} value={type.value}>{type.label}</option>)}
                          </SelectInput>
                        </div>
                        <div>
                          <FieldLabel>别名（逗号分隔）</FieldLabel>
                          <TextInput key={`char-aliases-${index}`} defaultValue={aliases.join(', ')} onBlur={(event) => updateCharacterField(index, 'aliases', event.currentTarget.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean))} placeholder="小名、英文名、昵称..." />
                        </div>
                        <div>
                          <FieldLabel>Danbooru Tag</FieldLabel>
                          <TextInput key={`char-danbooru-${index}`} defaultValue={String(character?.danbooru || '')} onBlur={(event) => updateCharacterField(index, 'danbooru', event.currentTarget.value)} placeholder="hatsune_miku" />
                        </div>
                        <div className="md:col-span-2">
                          <FieldLabel>外貌标签 appearance</FieldLabel>
                          <AutoResizeTextarea key={`char-appearance-${index}`} defaultValue={String(character?.appearance || '')} onBlur={(event) => updateCharacterField(index, 'appearance', event.currentTarget.value)} placeholder="long hair, blue eyes, white dress..." rows={3} />
                        </div>
                        <div className="md:col-span-2">
                          <FieldLabel>负向标签（可选）</FieldLabel>
                          <AutoResizeTextarea key={`char-negative-${index}`} defaultValue={String(character?.negative_tags || character?.negativeTags || '')} onBlur={(event) => updateCharacterField(index, 'negative_tags', event.currentTarget.value)} placeholder="需要避免的角色特征..." rows={2} />
                        </div>
                        <div className="md:col-span-2">
                          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                            <FieldLabel>角色服装</FieldLabel>
                            <Button onClick={() => addCharacterOutfit(index)}>+ 添加服装</Button>
                          </div>
                          {!outfits.length && <p className="mb-2 text-xs text-[var(--duties-secondary)]">未配置服装时可留空；添加后会在生图规划时作为服装参考发送给 AI。</p>}
                          <div className="space-y-2">
                            {outfits.map((outfit, outfitIndex) => (
                              <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-3" key={`${outfit.name || 'outfit'}-${outfitIndex}`}>
                                <div className="mb-2 flex items-center justify-between gap-2">
                                  <div className="font-mono text-xs font-semibold">服装 {outfitIndex + 1}</div>
                                  <Button onClick={() => deleteCharacterOutfit(index, outfitIndex)} variant="danger">删除服装</Button>
                                </div>
                                <div className="grid gap-2 md:grid-cols-3">
                                  <div>
                                    <FieldLabel>服装名称</FieldLabel>
                                    <TextInput key={`outfit-name-${index}-${outfitIndex}`} defaultValue={outfit.name} onBlur={(event) => updateCharacterOutfit(index, outfitIndex, 'name', event.currentTarget.value)} placeholder="常服、礼服、战斗服..." />
                                  </div>
                                  <div className="md:col-span-2">
                                    <FieldLabel>服装 TAG</FieldLabel>
                                    <AutoResizeTextarea key={`outfit-tags-${index}-${outfitIndex}`} defaultValue={outfit.tags} onBlur={(event) => updateCharacterOutfit(index, outfitIndex, 'tags', event.currentTarget.value)} placeholder="white shirt, pleated skirt, ribbon..." rows={2} />
                                  </div>
                                </div>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </Card>

          <Card title="原始配置编辑器" description={fileMeta(bundle, section)}>
            <div className="mb-3 grid gap-2 md:grid-cols-2">
              <FieldLabel>编辑对象</FieldLabel>
              <SelectInput onChange={(event) => setSection(event.currentTarget.value as SectionKey)} value={section}>
                {(Object.keys(sectionLabels) as SectionKey[]).map((key) => <option key={key} value={key}>{sectionLabels[key]}</option>)}
              </SelectInput>
            </div>
            <textarea
              className="h-[32rem] w-full resize-y border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 font-mono text-xs leading-5 text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
              onChange={(event) => setEditor(event.currentTarget.value)}
              spellCheck={false}
              value={editor}
            />
            <div className="mt-3 flex flex-wrap gap-2">
              <Button onClick={saveCurrent} variant="primary">保存当前文件</Button>
              <Button onClick={() => setEditor(section === 'node' ? nodeRaw : getSectionContent(bundle, section))}>撤销未保存修改</Button>
            </div>
          </Card>
        </>
      )}
    </PageShell>
  );
};
