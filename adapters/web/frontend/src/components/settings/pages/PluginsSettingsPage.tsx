import { useEffect, useRef, useState } from 'react';

import {
  applyPlugins, deletePlugin, getNodes, getPluginSource, getPlugins, getProviders,
  installPlugin, installPluginPackage, isEditConflict, updatePluginConfig, updatePluginSource,
  type AdminPlugin, type PluginParamSchema, type PluginRuntimeProcess,
  type PluginSelectors, type PluginsResponse,
} from '../../../api/supervisorClient';
import { useExclusiveAction } from '../../../hooks/useExclusiveAction';
import { usePageNotice } from '../../../hooks/usePageNotice';
import { confirmNavigation, useUnsavedChanges } from '../../../hooks/useUnsavedChanges';
import { tokenChanged, useSettingsStore } from '../../../store/settingsStore';
import { useBrowserPlugins } from '../../../plugins/status';
import { refreshFrontendPlugins } from '../../../plugins/runtime';
import {
  Block, Button, Check, Chip, Chips, Empty, Field, Input, PageShell, Pre,
  ReadOnlyRow, Select, StatusBar, Tag, Textarea,
} from './settingsControls';

type Choices = Record<keyof PluginSelectors, string[]>;
type Action = (action: () => Promise<void>) => Promise<void>;
const EMPTY_CHOICES: Choices = { nodes: [], channels: [], providers: [], purposes: [] };
const SELECTOR_LABELS: Record<keyof PluginSelectors, string> = {
  nodes: '节点', channels: '模型渠道', providers: 'Provider', purposes: '请求用途',
};
const STATUS_LABELS = { loaded: '已加载', disabled: '已停用', error: '运行异常', retiring: '等待在途请求结束', stopped: '进程已离线', not_applicable: '不在此进程运行' };
const json = (value: unknown) => JSON.stringify(value, null, 2);
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '操作失败';

function configObject(text: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(text);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('参数 JSON 必须是对象');
  return parsed as Record<string, unknown>;
}

function paramKey(param: PluginParamSchema): string { return typeof param.key === 'string' ? param.key : typeof param.name === 'string' ? param.name : ''; }
function paramKind(param: PluginParamSchema): string { return typeof param.type === 'string' ? param.type : typeof param.kind === 'string' ? param.kind : 'text'; }
function paramLabel(param: PluginParamSchema): string { return typeof param.label === 'string' ? param.label : paramKey(param); }
function paramDescription(param: PluginParamSchema): string { return typeof param.description === 'string' ? param.description : typeof param.desc === 'string' ? param.desc : ''; }
const KNOWN_KINDS = new Set(['text', 'string', 'password', 'textarea', 'bool', 'boolean', 'toggle', 'int', 'integer', 'float', 'number', 'enum', 'select']);

function paramChoices(param: PluginParamSchema): Array<{ value: unknown; label: string }> {
  const options = param.options ?? param.choices;
  return (Array.isArray(options) ? options : []).map((choice) => {
    if (choice && typeof choice === 'object' && 'value' in choice) {
      const entry = choice as { value: unknown; label?: unknown };
      return { value: entry.value, label: String(entry.label ?? entry.value) };
    }
    return { value: choice, label: String(choice) };
  });
}

function validateParams(config: Record<string, unknown>, schema: PluginParamSchema[]): string {
  for (const param of schema) {
    const key = paramKey(param);
    const value = Object.prototype.hasOwnProperty.call(config, key) ? config[key] : param.default;
    if (!key) continue;
    const kind = paramKind(param);
    const label = paramLabel(param);
    if (value === undefined) {
      if (param.required) return `缺少插件参数：${label}`;
      continue;
    }
    if (['int', 'integer', 'float', 'number'].includes(kind)) {
      if (typeof value !== 'number' || !Number.isFinite(value)) return `${label}必须是数字`;
      if (['int', 'integer'].includes(kind) && !Number.isInteger(value)) return `${label}必须是整数`;
      if (typeof param.minimum === 'number' && value < param.minimum) return `${label}不能小于 ${param.minimum}`;
      if (typeof param.maximum === 'number' && value > param.maximum) return `${label}不能大于 ${param.maximum}`;
    }
    if (['bool', 'boolean', 'toggle'].includes(kind) && typeof value !== 'boolean') return `${label}必须是布尔值`;
    if (['text', 'string', 'password', 'textarea'].includes(kind) && typeof value !== 'string') return `${label}必须是文本`;
    if (['select', 'enum'].includes(kind) && Array.isArray(param.options ?? param.choices) && !paramChoices(param).some((choice) => JSON.stringify(choice.value) === JSON.stringify(value))) return `${label}不在可选值中`;
  }
  return '';
}

function RuntimeStatus({ plugin, runtime, changed = false }: { plugin: AdminPlugin; runtime: PluginRuntimeProcess[]; changed?: boolean }) {
  if (changed) return <Tag>配置已在别处更新</Tag>;
  const active = runtime.filter((process) => process.alive);
  if (!active.length) return <Tag>等待运行进程上报</Tag>;
  if (plugin.enabled && active.every((process) => process.plugins.some((entry) => entry.id === plugin.id && entry.status === 'not_applicable'))) return <Tag>等待适用进程上报</Tag>;
  const pending = active.some((process) => !process.plugins.some((entry) => entry.id === plugin.id && entry.desired_hash === plugin.desired_hash && (entry.status === 'not_applicable' || entry.status === (plugin.enabled ? 'loaded' : 'disabled'))));
  const failed = active.some((process) => process.plugins.some((entry) => entry.id === plugin.id && (entry.status === 'error' || entry.errors?.length)));
  return <Tag>{failed ? '运行异常' : pending ? '等待进程更新 / 上报' : plugin.enabled ? '已应用 · 启用' : '已应用 · 停用'}</Tag>;
}

function BrowserStatus({ plugin }: { plugin: AdminPlugin }) {
  const browser = useBrowserPlugins();
  const entry = browser.plugins.find((item) => item.id === plugin.id);
  if (!plugin.frontend?.entry && !entry) return null;
  return <Block title="浏览器实际状态">
    <ReadOnlyRow name="状态" value={entry ? { loaded: '已加载', error: '运行异常', loading: '正在加载', disabled: '已停用' }[entry.status] : plugin.enabled ? '等待浏览器加载' : '已停用'} />
    <ReadOnlyRow name="实际版本" value={entry?.activeHash || '尚未加载'} />
    <ReadOnlyRow name="目标版本" value={plugin.desired_hash} />
    <ReadOnlyRow name="注册代次" value={entry?.generation || '无'} />
    {!!entry?.activeHash && entry.activeHash !== plugin.desired_hash && <p role="status">浏览器仍在使用上一可用版本。</p>}
    {entry?.errors.map((error, index) => <Pre key={index}>{error}</Pre>)}
    {browser.error && <Pre>{browser.error}</Pre>}
  </Block>;
}

function changedPluginIds(before: AdminPlugin[], after: AdminPlugin[]): string[] {
  const hashes = new Map(after.map((plugin) => [plugin.id, plugin.desired_hash]));
  const changes = before.filter((plugin) => hashes.get(plugin.id) !== plugin.desired_hash).map((plugin) => plugin.id);
  return [...changes, ...after.filter((plugin) => !before.some((item) => item.id === plugin.id)).map((plugin) => plugin.id)];
}

function runtimePending(data: PluginsResponse, changed: string[]): boolean {
  const processes = data.runtime.filter((process) => process.alive);
  return processes.some((process) => (
    process.plugins.some((entry) => (entry.active_requests || 0) > 0 || (
      !changed.includes(entry.id) && !data.plugins.some((plugin) => plugin.id === entry.id)
    )) || data.plugins.some((plugin) => {
      if (changed.includes(plugin.id)) return false;
      const entry = process.plugins.find((item) => item.id === plugin.id);
      if (!entry || entry.desired_hash !== plugin.desired_hash) return true;
      return entry.status === 'retiring' || (entry.status !== 'not_applicable' && entry.status !== 'error' && entry.status !== (plugin.enabled ? 'loaded' : 'disabled'));
    })
  ));
}

function SelectorField({ name, values, choices, disabled, onChange }: {
  name: keyof PluginSelectors; values: string[]; choices: string[]; disabled: boolean; onChange: (values: string[]) => void;
}) {
  const [custom, setCustom] = useState('');
  const label = SELECTOR_LABELS[name];
  const add = (value: string) => {
    const next = value.trim();
    if (next && !values.includes(next)) onChange([...values, next]);
    setCustom('');
  };
  return (
    <div className="space-y-2">
      <Field htmlFor={`plugin-selector-${name}`} label={label} layout="stacked">
        <Select disabled={disabled} id={`plugin-selector-${name}`} onChange={(event) => add(event.target.value)} value="" width="wide">
          <option value="">添加{label}…</option>
          {choices.filter((choice) => !values.includes(choice)).map((choice) => <option key={choice} value={choice}>{choice}</option>)}
        </Select>
      </Field>
      <Chips>{values.map((value) => <Chip key={value} onRemove={disabled ? undefined : () => onChange(values.filter((entry) => entry !== value))} removeLabel={`移除${label} ${value}`}>{value}</Chip>)}</Chips>
      <div className="flex flex-wrap gap-2">
        <Input aria-label={`自定义${label}`} disabled={disabled} onChange={(event) => setCustom(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); add(custom); } }} placeholder={`输入${label}标识`} value={custom} width="flex" />
        <Button disabled={disabled || !custom.trim()} onClick={() => add(custom)} size="sm" tone="quiet">添加{label}</Button>
      </div>
      {!values.length && <p className="text-xs text-[var(--duties-secondary)]">不限制{label}</p>}
    </div>
  );
}

function PluginDetail({ plugin, token, choices, runtime, changed, busy, run, onUpdate, onDelete, current }: {
  plugin: AdminPlugin; token: string; choices: Choices; runtime: PluginRuntimeProcess[];
  changed: boolean;
  busy: boolean; run: Action; onUpdate: (plugin: AdminPlugin) => void;
  onDelete: (plugin: AdminPlugin) => Promise<void>; current: () => boolean;
}) {
  const [enabled, setEnabled] = useState(plugin.enabled);
  const [config, setConfig] = useState(json(plugin.config));
  const [selectors, setSelectors] = useState(plugin.selectors);
  const [configHash, setConfigHash] = useState(plugin.desired_hash);
  const [savedConfig, setSavedConfig] = useState(json({ enabled: plugin.enabled, config: json(plugin.config), selectors: plugin.selectors }));
  const [source, setSource] = useState('');
  const [savedSource, setSavedSource] = useState('');
  const [sourceHash, setSourceHash] = useState(plugin.source_hash);
  const [sourceLoading, setSourceLoading] = useState(true);
  const [sourceError, setSourceError] = useState('');
  const [sourceReadable, setSourceReadable] = useState(false);
  const [sourceAttempt, setSourceAttempt] = useState(0);
  const [message, setMessage] = useState('');
  const configDirty = json({ enabled, config, selectors }) !== savedConfig;
  const sourceDirty = source !== savedSource;
  useUnsavedChanges(configDirty || sourceDirty);

  useEffect(() => {
    if (plugin.kind === 'package') { setSourceLoading(false); return; }
    let cancelled = false;
    setSourceLoading(true);
    setSourceError('');
    void getPluginSource(token, plugin.id).then((result) => {
      if (cancelled || !current()) return;
      setSource(result.content);
      setSavedSource(result.content);
      setSourceHash(result.hash);
      setSourceReadable(true);
    }).catch((error: unknown) => {
      if (!cancelled && current()) setSourceError(errorMessage(error));
    }).finally(() => { if (!cancelled && current()) setSourceLoading(false); });
    return () => { cancelled = true; };
  }, [token, plugin.id, plugin.kind, sourceAttempt]);

  let parsed: Record<string, unknown> | null = null;
  let validation = '';
  try { parsed = configObject(config); validation = enabled ? validateParams(parsed, plugin.params_schema) : ''; } catch (error) { validation = errorMessage(error); }
  const setParam = (key: string, value: unknown) => {
    if (parsed) setConfig(json({ ...parsed, [key]: value }));
  };
  const saveConfig = () => run(async () => {
    if (!parsed || validation || !current()) return;
    try {
      const result = await updatePluginConfig(token, plugin.id, { enabled, config: parsed, selectors, base_hash: configHash });
      if (!current()) return;
      onUpdate(result);
      setEnabled(result.enabled);
      setConfig(json(result.config));
      setSelectors(result.selectors);
      setConfigHash(result.desired_hash);
      setSavedConfig(json({ enabled: result.enabled, config: json(result.config), selectors: result.selectors }));
      setMessage('配置已保存，运行进程会自动更新；可立即应用并查看状态。');
      void refreshFrontendPlugins();
    } catch (error) {
      if (current()) setMessage(isEditConflict(error) ? '插件已被其他页面修改。草稿已保留，请复制后刷新并核对。' : errorMessage(error));
    }
  });
  const saveSource = () => run(async () => {
    if (!current()) return;
    try {
      const result = await updatePluginSource(token, plugin.id, source, sourceHash);
      if (!current()) return;
      onUpdate(result);
      setSavedSource(source);
      setSourceHash(result.source_hash);
      setSourceReadable(true);
      const nextConfig = json({ enabled: result.enabled, config: json(result.config), selectors: result.selectors });
      if (!configDirty) {
        setEnabled(result.enabled);
        setConfig(json(result.config));
        setSelectors(result.selectors);
        setSavedConfig(nextConfig);
        setConfigHash(result.desired_hash);
      } else if (nextConfig === savedConfig) setConfigHash(result.desired_hash);
      setMessage(configDirty && nextConfig !== savedConfig
        ? '源码已保存，但参数定义或配置已变化。运行进程会自动更新；配置草稿已保留，请复制后刷新并核对，再保存配置。'
        : '源码已保存，运行进程会自动更新；可立即应用并查看状态。');
    } catch (error) {
      if (current()) setMessage(isEditConflict(error) ? '源码已被其他页面修改。草稿已保留，请复制后刷新并核对。' : errorMessage(error));
    }
  });
  const replaceFile = (file: File | undefined) => {
    if (!file) return;
    void run(async () => {
      if (!/\.py$/i.test(file.name)) { setMessage('请选择 .py 插件文件'); return; }
      if (sourceDirty && !window.confirm('替换文件会覆盖尚未保存的源码，确定继续吗？')) return;
      try {
        const content = await file.text();
        if (current()) { setSource(content); setSourceError(''); setMessage('替换的源码尚未保存。'); }
      } catch (error) { if (current()) setMessage(errorMessage(error)); }
    });
  };
  const replacePackage = (file: File | undefined) => {
    if (!file) return;
    void run(async () => {
      if (!/\.zip$/i.test(file.name)) { setMessage('请选择 .zip 插件包'); return; }
      if (!current() || !confirmNavigation()) return;
      try {
        const result = await installPluginPackage(token, file, configHash, current);
        if (!current()) return;
        if (result.id !== plugin.id) throw new Error('替换包的插件标识与当前插件不同');
        onUpdate(result); setConfigHash(result.desired_hash);
        setEnabled(result.enabled); setConfig(json(result.config)); setSelectors(result.selectors);
        setSavedConfig(json({ enabled: result.enabled, config: json(result.config), selectors: result.selectors }));
        setMessage('插件包已替换，浏览器和运行进程会分别应用新版本。');
        void refreshFrontendPlugins();
      } catch (error) { if (current()) setMessage(errorMessage(error)); }
    });
  };

  return (
    <div className="space-y-5" aria-label={`插件详情 ${plugin.name}`}>
      <Block title={plugin.name} hint={plugin.description} frame="boxed">
        <Chips><Tag>{plugin.source === 'custom' ? '自定义插件' : '内置插件'}</Tag><RuntimeStatus changed={changed} plugin={plugin} runtime={runtime} /></Chips>
        <ReadOnlyRow name="版本" value={plugin.version} />
        <ReadOnlyRow name="作者" value={plugin.author} />
        <ReadOnlyRow name="文件" value={plugin.filename} />
        <ReadOnlyRow name="插件标识" value={plugin.id} />
        {plugin.error && <p className="mt-2 whitespace-pre-wrap break-words text-[var(--duties-danger)]" role="alert">{plugin.error}</p>}
      </Block>
      {message && <p className="whitespace-pre-wrap break-words text-[var(--duties-secondary)]" role="status">{message}</p>}
      <Block title="启用与参数" hint="保存后运行进程会自动更新；启用状态与下方进程实际加载状态分别展示。">
        <fieldset className="min-w-0 space-y-4" disabled={busy}>
          <Check checked={enabled} onChange={setEnabled}>启用插件</Check>
          {plugin.params_schema.filter((param) => paramKey(param) && KNOWN_KINDS.has(paramKind(param))).map((param) => {
            const key = paramKey(param);
            const kind = paramKind(param);
            const value = parsed?.[key] ?? param.default;
            const label = paramLabel(param);
            const options = paramChoices(param);
            const id = `plugin-param-${key}`;
            return (
              <div className="space-y-1" key={key}>
                {['bool', 'boolean', 'toggle'].includes(kind) ? (
                  <Check checked={value === true} disabled={!parsed} onChange={(next) => setParam(key, next)}>{label}</Check>
                ) : (
                  <Field htmlFor={id} label={label} layout="stacked">
                    {['select', 'enum'].includes(kind) ? (
                      <Select disabled={!parsed} id={id} onChange={(event) => setParam(key, options[Number(event.target.value)].value)} value={String(options.findIndex((option) => JSON.stringify(option.value) === JSON.stringify(value)))} width="wide">
                        <option value="-1" disabled>{value === undefined ? '使用默认值' : `当前值：${String(value)}`}</option>
                        {options.map((option, index) => <option key={index} value={String(index)}>{option.label}</option>)}
                      </Select>
                    ) : ['int', 'integer', 'float', 'number'].includes(kind) ? (
                      <Input disabled={!parsed} id={id} max={typeof param.maximum === 'number' ? param.maximum : undefined} min={typeof param.minimum === 'number' ? param.minimum : undefined} onChange={(event) => {
                        if (event.target.value === '') {
                          const next = { ...parsed }; delete next[key]; setConfig(json(next));
                        } else setParam(key, Number(event.target.value));
                      }} step={['int', 'integer'].includes(kind) ? 1 : 'any'} type="number" value={typeof value === 'number' ? value : ''} />
                    ) : kind === 'textarea' ? (
                      <Textarea disabled={!parsed} id={id} onChange={(event) => setParam(key, event.target.value)} rows={4} value={typeof value === 'string' ? value : ''} />
                    ) : <Input disabled={!parsed} id={id} onChange={(event) => setParam(key, event.target.value)} type={kind === 'password' ? 'password' : 'text'} value={typeof value === 'string' ? value : ''} width="wide" />}
                  </Field>
                )}
                {paramDescription(param) && <p className="text-xs text-[var(--duties-secondary)]">{paramDescription(param)}</p>}
              </div>
            );
          })}
          <details>
            <summary className="cursor-pointer text-[var(--duties-secondary)]">高级参数 JSON（包含未声明的参数）</summary>
            <Textarea aria-label="插件参数 JSON" className="mt-2 font-mono text-xs" onChange={(event) => setConfig(event.target.value)} rows={8} value={config} />
          </details>
          {validation && <p role="alert" className="text-[var(--duties-danger)]">{validation}</p>}
          <Block title="作用范围" hint="每项留空表示不限制；多项条件同时满足时才运行插件。">
            <div className="grid gap-4 sm:grid-cols-2">
              {(Object.keys(SELECTOR_LABELS) as Array<keyof PluginSelectors>).map((name) => <SelectorField choices={choices[name]} disabled={busy} key={name} name={name} onChange={(values) => setSelectors({ ...selectors, [name]: values })} values={selectors[name]} />)}
            </div>
          </Block>
        </fieldset>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-xs text-[var(--duties-secondary)]">{configDirty ? '配置有未保存的修改' : '配置已保存'}</span>
          <Button disabled={busy || !configDirty} onClick={() => { const saved = JSON.parse(savedConfig); setEnabled(saved.enabled); setConfig(saved.config); setSelectors(saved.selectors); }} tone="quiet">丢弃配置修改</Button>
          <Button disabled={busy || !configDirty || !parsed || !!validation} onClick={() => void saveConfig()}>保存配置</Button>
        </div>
      </Block>
      {plugin.kind === 'package' ? <Block title="插件包文件" hint="使用完整 ZIP 包替换版本，保留旧版本供在途请求读取。">
        <Pre>{plugin.files?.map((file) => `${file.path} · ${file.size} bytes`).join('\n') || plugin.filename}</Pre>
        {plugin.source === 'custom' && <Input accept=".zip,application/zip" aria-label="替换插件包" disabled={busy} onChange={(event) => { replacePackage(event.target.files?.[0]); event.target.value = ''; }} type="file" width="flex" />}
      </Block> : <Block title="Python 源码" hint={plugin.source === 'bundled' ? '内置插件源码只读。' : '替换 .py 文件或直接编辑，保存后运行进程会自动更新。'}>
        {sourceLoading ? <p>正在读取源码…</p> : sourceError ? <div><p role="alert">{sourceError}</p><Button disabled={busy} onClick={() => setSourceAttempt((value) => value + 1)} tone="quiet">重试读取源码</Button></div> : (
          <>
            <Textarea aria-label="插件 Python 源码" className="font-mono text-xs" disabled={busy} onChange={(event) => setSource(event.target.value)} readOnly={plugin.source === 'bundled'} rows={16} spellCheck={false} value={source} />
          </>
        )}
        {plugin.source === 'custom' && !sourceLoading && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Input accept=".py,text/x-python" aria-label="替换插件文件" disabled={busy} onChange={(event) => { replaceFile(event.target.files?.[0]); event.target.value = ''; }} type="file" width="flex" />
            <Button disabled={busy || !sourceDirty} onClick={() => { setSource(savedSource); if (!sourceReadable) setSourceAttempt((value) => value + 1); }} tone="quiet">丢弃源码修改</Button>
            <Button disabled={busy || !sourceDirty || !sourceHash} onClick={() => void saveSource()}>保存源码</Button>
          </div>
        )}
      </Block>}
      <BrowserStatus plugin={plugin} />
      <Block title="进程实际状态" hint="进程确认加载后才算生效；旧请求可继续使用旧版本，结束后卸载。">
        {!runtime.length && <Empty>暂未收到运行进程状态</Empty>}
        {runtime.map((process) => {
          const entry = process.plugins.find((item) => item.id === plugin.id);
          return (
            <div className="space-y-2 border-b border-[var(--duties-border)] py-3" key={`${process.process}-${process.pid}`}>
              <p>{process.process} · PID {process.pid} · {process.alive ? entry ? STATUS_LABELS[entry.status] : '尚未上报此插件' : '进程已离线'}</p>
              <p className="text-xs text-[var(--duties-secondary)]">{entry?.desired_hash === plugin.desired_hash ? '当前保存版本' : '与当前保存版本不同'} · 上报于 {process.updated_at || '未知'}</p>
              {!!entry?.active_requests && <p className="text-xs text-[var(--duties-secondary)]">{entry.active_requests} 个请求仍在使用此插件</p>}
              {entry?.error && <Pre>{entry.error}</Pre>}
              {entry?.errors?.map((error, index) => <Pre key={index}>{`${error.at} · ${error.phase}\n${error.message}`}</Pre>)}
            </div>
          );
        })}
      </Block>
      <details><summary className="cursor-pointer text-[var(--duties-secondary)]">插件元数据</summary><Pre className="mt-2">{json(plugin.metadata)}</Pre></details>
      {plugin.source === 'custom' && <Button disabled={busy} onClick={() => void onDelete(plugin)} tone="danger">删除插件</Button>}
    </div>
  );
}

function PluginsWorkspace({ token }: { token: string }) {
  const [data, setData] = useState<PluginsResponse | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [detailVersion, setDetailVersion] = useState(0);
  const [choices, setChoices] = useState<Choices>(EMPTY_CHOICES);
  const [changedPlugins, setChangedPlugins] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  const loadVersion = useRef(0);
  const pollRequest = useRef<AbortController | null>(null);
  const { busy, run: runExclusive } = useExclusiveAction();
  const { notice, setMessage, setLoadError, clearNotice } = usePageNotice();
  const current = () => mounted.current && !tokenChanged(token) && useSettingsStore.getState().isAuthenticated;
  const run: Action = (action) => runExclusive(async () => {
    pollRequest.current?.abort();
    await action();
  });

  const load = async (reset = false) => {
    const version = ++loadVersion.current;
    setLoading(true);
    try {
      const result = await getPlugins(token);
      if (!current() || version !== loadVersion.current) return;
      setData(result);
      setChangedPlugins([]);
      setSelectedId((id) => result.plugins.some((plugin) => plugin.id === id) ? id : result.plugins[0]?.id || '');
      if (reset) setDetailVersion((value) => value + 1);
      setLoadError('', 'catalog');
    } catch (error) { if (current() && version === loadVersion.current) setLoadError(errorMessage(error), 'catalog'); }
    finally { if (current() && version === loadVersion.current) setLoading(false); }
  };

  useEffect(() => {
    mounted.current = true;
    void load();
    void Promise.allSettled([getNodes(token), getProviders(token)]).then(([nodes, providers]) => {
      if (!current()) return;
      setChoices({
        nodes: nodes.status === 'fulfilled' ? nodes.value.map((node) => node.id) : [],
        channels: providers.status === 'fulfilled' ? Object.keys(providers.value.providers || {}) : [],
        providers: providers.status === 'fulfilled' ? providers.value.registered || [] : [],
        purposes: ['inference', 'system', 'fallback', 'diagnostic'],
      });
      const failures = [nodes, providers].filter((result) => result.status === 'rejected');
      if (failures.length) setLoadError('部分作用范围候选项加载失败，可手动输入标识。', 'choices');
    });
    return () => { mounted.current = false; loadVersion.current += 1; };
  }, [token]);

  useEffect(() => {
    if (!data || busy || loading || !runtimePending(data, changedPlugins)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      pollRequest.current = controller;
      void getPlugins(token, controller.signal).then((result) => {
        if (controller.signal.aborted || !current()) return;
        setData((previous) => previous ? { ...previous, runtime: result.runtime } : previous);
        setChangedPlugins(changedPluginIds(data.plugins, result.plugins));
        setLoadError('', 'runtime');
      }).catch((error: unknown) => {
        if (!controller.signal.aborted && current()) setLoadError(errorMessage(error), 'runtime');
      }).finally(() => { if (pollRequest.current === controller) pollRequest.current = null; });
    }, 3000);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [token, data, busy, loading, changedPlugins]);

  const update = (plugin: AdminPlugin) => {
    setChangedPlugins((previous) => previous.filter((id) => id !== plugin.id));
    setData((previous) => previous ? {
      ...previous, plugins: previous.plugins.some((item) => item.id === plugin.id)
        ? previous.plugins.map((item) => item.id === plugin.id ? plugin : item) : [...previous.plugins, plugin],
    } : previous);
  };
  const upload = () => run(async () => {
    if (!file || !current() || !confirmNavigation()) return;
    if (!/\.(py|zip)$/i.test(file.name)) { setMessage('请选择 .py 插件文件或 .zip 插件包'); return; }
    try {
      const content = /\.py$/i.test(file.name) ? await file.text() : '';
      if (!current()) return;
      const result = /\.zip$/i.test(file.name) ? await installPluginPackage(token, file, undefined, current) : await installPlugin(token, file.name, content);
      if (!current()) return;
      update(result);
      setSelectedId(result.id);
      setFile(null);
      if (fileInput.current) fileInput.current.value = '';
      setMessage('插件已导入，默认停用。检查参数和作用范围后启用并保存，运行进程会自动更新。');
    } catch (error) { if (current()) setMessage(errorMessage(error)); }
  });
  const remove = (plugin: AdminPlugin) => run(async () => {
    if (!confirmNavigation() || !window.confirm(`确定删除插件「${plugin.name}」及其配置吗？`)) return;
    try {
      await deletePlugin(token, plugin.id, plugin.desired_hash);
      if (!current()) return;
      setData((previous) => previous ? { ...previous, plugins: previous.plugins.filter((item) => item.id !== plugin.id) } : previous);
      setChangedPlugins((previous) => previous.filter((id) => id !== plugin.id));
      setSelectedId('');
      setMessage('插件已删除，运行进程会自动卸载；正在进行的请求会使用原版本完成。');
      void refreshFrontendPlugins();
    } catch (error) { if (current()) setMessage(errorMessage(error)); }
  });
  const apply = () => run(async () => {
    if (!current()) return;
    try {
      const result = await applyPlugins(token);
      if (!current()) return;
      setData((previous) => previous ? { ...previous, generation: result.generation, runtime: result.runtime } : previous);
      setMessage(result.message || '已发出应用请求，请刷新进程状态确认结果。');
      void refreshFrontendPlugins();
    } catch (error) { if (current()) setMessage(errorMessage(error)); }
  });
  const refreshRuntime = () => run(async () => {
    void refreshFrontendPlugins();
    try {
      const result = await getPlugins(token);
      if (current()) {
        setData((previous) => previous ? { ...previous, runtime: result.runtime } : previous);
        setChangedPlugins(data ? changedPluginIds(data.plugins, result.plugins) : []);
        setLoadError('', 'runtime');
      }
    } catch (error) { if (current()) setLoadError(errorMessage(error), 'runtime'); }
  });
  const selected = data?.plugins.find((plugin) => plugin.id === selectedId);
  const keyword = query.trim().toLowerCase();
  const visible = data?.plugins.filter((plugin) => [plugin.id, plugin.name, plugin.description, plugin.filename].some((value) => value.toLowerCase().includes(keyword))) || [];
  const locked = busy || loading;
  return (
    <PageShell title="插件" description="导入 Python 插件或系统插件包，设置参数与作用范围，查看各进程和浏览器实际加载的版本。" notice={notice} bar={
      <StatusBar status={busy ? '正在处理…' : loading ? '正在读取…' : `${data?.plugins.length || 0} 个插件`} tone={data ? 'live' : 'idle'}>
        <Button disabled={locked} onClick={() => void refreshRuntime()} size="sm" tone="quiet">刷新进程状态</Button>
        <Button disabled={locked || !data} onClick={() => void apply()} size="sm">应用已保存的更改</Button>
      </StatusBar>
    }>
      {data?.shared_group && <p className="text-[var(--duties-secondary)]">共享组「{data.shared_group}」：插件文件和配置由组内实例共享；各实例分别确认运行状态。</p>}
      {!!changedPlugins.length && <p role="status" className="text-[var(--duties-secondary)]">插件文件或配置已在其他位置更新。当前草稿保持不变，请刷新列表查看最新配置。</p>}
      <Block title="导入插件" hint="上传 .py 文件或包含 plugin.json 的 .zip 插件包后默认停用；同名插件请在详情中替换文件。">
        <div className="flex flex-wrap items-center gap-2">
          <Input accept=".py,.zip,text/x-python,application/zip" aria-label="导入插件文件" disabled={locked} onChange={(event) => setFile(event.target.files?.[0] || null)} ref={fileInput} type="file" width="flex" />
          <Button disabled={locked || !file || !data} onClick={() => void upload()}>导入插件</Button>
        </div>
      </Block>
      <Block title="插件列表">
        <div className="mb-3 flex flex-wrap gap-2">
          <Input aria-label="筛选插件" onChange={(event) => setQuery(event.target.value)} placeholder="名称、描述或文件名" value={query} width="flex" />
          <Button disabled={locked} onClick={() => { if (confirmNavigation()) { clearNotice(); void run(() => load(true)); } }} tone="quiet">刷新列表</Button>
        </div>
        {!loading && !visible.length && <Empty>{data?.plugins.length ? '没有匹配的插件' : '尚无插件，请导入 .py 文件'}</Empty>}
        <div className="grid gap-2 sm:grid-cols-2">
          {visible.map((plugin) => (
            <button aria-pressed={selectedId === plugin.id} className={`min-w-0 space-y-2 border p-3 text-left ${selectedId === plugin.id ? 'border-[var(--duties-text)] bg-[var(--duties-muted)]' : 'border-[var(--duties-border)] bg-[var(--duties-panel)]'}`} disabled={locked} key={plugin.id} onClick={() => { if (selectedId !== plugin.id && confirmNavigation()) setSelectedId(plugin.id); }} type="button">
              <span className="block break-words font-medium">{plugin.name} <span className="text-xs text-[var(--duties-secondary)]">{plugin.version}</span></span>
              <span className="block break-words text-xs text-[var(--duties-secondary)]">{plugin.description || plugin.filename}</span>
              <span className="flex flex-wrap gap-1.5"><Tag>{plugin.source === 'custom' ? '自定义' : '内置'}</Tag><Tag>{plugin.enabled ? '配置：启用' : '配置：停用'}</Tag><RuntimeStatus changed={changedPlugins.includes(plugin.id)} plugin={plugin} runtime={data?.runtime || []} /></span>
            </button>
          ))}
        </div>
      </Block>
      {selected && <PluginDetail busy={locked} changed={changedPlugins.includes(selected.id)} choices={choices} current={current} key={`${selected.id}-${detailVersion}`} onDelete={remove} onUpdate={update} plugin={selected} run={run} runtime={data?.runtime || []} token={token} />}
    </PageShell>
  );
}

export function PluginsSettingsPage() {
  const { adminToken, isAuthenticated } = useSettingsStore();
  return adminToken && isAuthenticated ? <PluginsWorkspace key={adminToken} token={adminToken} /> : (
    <PageShell authed={false} title="插件" description="导入和管理 Python 插件。"><span /></PageShell>
  );
}
