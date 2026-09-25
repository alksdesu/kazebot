// 模型参数：各家 provider 的可调项。清单由 provider 类自己声明、后端公布，这一页只渲染和写回。
// 全局写 runtime.yaml 的 providers.<类型>.options，节点写节点文件的 provider_options，节点优先。
import { useEffect, useMemo, useState } from 'react';
import { confirmNavigation, useUnsavedChanges } from '../hooks/useUnsavedChanges';

import {
  getNodeRaw,
  channelChoices,
  getNodes,
  getProviderProfiles,
  getProviders,
  getRuntimeRaw,
  updateNodeRaw,
  updateRuntimeRaw,
  type ProviderOptionsCatalog,
  type ProvidersResponse,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Empty, Facts, Footnote, Panel, SaveBar, Segmented } from './components';
import { FallbackChain } from './FallbackChain';
import { NodeYamlShapeError, readYamlScalar, upsertYamlNested } from './nodeYaml';
import { OptionRow, toYamlScalar, visibleSpecs } from './optionFields';
import { wireOf } from './channelFields';

const GLOBAL = '__global__';

const say = (error: unknown): string => (error instanceof Error ? error.message : String(error));

interface NodeChoice {
  id: string;
  provider: string;
}

export const ModelsPage = () => {
  const token = useSettingsStore(state => state.adminToken);
  return <ModelsPageEditor key={token || ""} />;
};

const ModelsPageEditor = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [catalog, setCatalog] = useState<ProviderOptionsCatalog>({});
  const [providers, setProviders] = useState<ProvidersResponse | null>(null);
  const [nodes, setNodes] = useState<NodeChoice[]>([]);
  const [scope, setScope] = useState<string>(GLOBAL);
  const [provider, setProvider] = useState('');
  const [raw, setRaw] = useState('');
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [note, setNote] = useState('');
  // 清单和原文是两条独立的加载线，共用一个提示位时后到的成功会把先到的失败抹掉。
  const [catalogError, setCatalogError] = useState('');

  useEffect(() => {
    if (!token) return;
    void (async () => {
      try {
        setCatalog((await getProviderProfiles(token)).options);
        setCatalogError('');
      } catch (error) {
        setCatalogError(say(error));
      }
      void getProviders(token).then(setProviders).catch(() => undefined);
      void getNodes(token)
        .then((list) => setNodes(
          list.map((item: any) => ({ id: String(item.id), provider: String(item.provider || '') }))
            .filter((item: NodeChoice) => item.id),
        ))
        .catch(() => undefined);
    })();
  }, [token]);

  // 换作用域要重新取那一份原文，草稿一并丢掉——它属于上一个文件。
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setEdits({});
    setRaw(''); setLoading(true);
    void (async () => {
      try {
        const body = scope === GLOBAL ? await getRuntimeRaw(token) : await getNodeRaw(token, scope);
        if (cancelled) return;
        setRaw(body);
        setNote('');
      } catch (error) {
        if (cancelled) return;
        setRaw('');
        setNote(say(error));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [token, scope]);

  const providerNames = useMemo(() => Object.keys(catalog).sort(), [catalog]);
  const activeProvider = providers?.active_provider || 'openai';
  const channels = channelChoices(providers || undefined).channels;
  const activeWire = wireOf(activeProvider, channels);

  // 节点没写 provider 就跟随全局活跃渠道，和 engine 的解析顺序一致。
  const scopeProvider = useMemo(() => {
    if (scope === GLOBAL) return '';
    const hit = nodes.find((item) => item.id === scope);
    return hit?.provider || readYamlScalar(raw, ['provider']) || activeProvider;
  }, [scope, nodes, raw, activeProvider]);

  const selected = scope === GLOBAL
    ? (provider || activeWire)
    : wireOf(scopeProvider || activeProvider, channels);
  const specs = catalog[selected] || [];

  const scopeChoices: ReadonlyArray<readonly [string, string]> = [
    [GLOBAL, '全局'],
    ...nodes.map((item) => [item.id, item.id] as const),
  ];
  const providerChoices: ReadonlyArray<readonly [string, string]> = providerNames.map(
    (name) => [name, name === activeWire ? `${name} ·在用` : name] as const,
  );

  const pathFor =(key: string): string[] => (
    scope === GLOBAL ? ['providers', selected, 'options', key] : ['provider_options', key]
  );

  const stored = (key: string): string => (raw ? readYamlScalar(raw, pathFor(key)) : '');
  const shownOf = (key: string): string => (key in edits ? edits[key] : stored(key));

  const visible = visibleSpecs(specs, shownOf);

  const dirty = Object.entries(edits).some(([key, value]) => value !== stored(key));
  const confirmDiscard = useUnsavedChanges(dirty, '模型参数还有未保存修改，确定切换或重置吗？');

  const save = async () => {
    if (!token || !raw || busy || loading) return;
    setBusy(true);
    try {
      let next = raw;
      for (const spec of specs) {
        if (!(spec.key in edits)) continue;
        next = upsertYamlNested(next, pathFor(spec.key), toYamlScalar(spec, edits[spec.key]));
      }
      if (scope === GLOBAL) await updateRuntimeRaw(token, next);
      else await updateNodeRaw(token, scope, next);
      setRaw(next);
      setEdits({});
      setNote(scope === GLOBAL
        ? '已保存。runtime.yaml 每次任务重新读，下一条消息就用新参数。'
        : '已保存。节点文件每次任务重新读，立刻生效。');
    } catch (error) {
      setNote(error instanceof NodeYamlShapeError
        ? `${error.message}（没有写入，文件原样保留）`
        : say(error));
    }
    setBusy(false);
  };

  if (!providerNames.length) {
    return <Empty>{catalogError || '正在读取参数清单…'}</Empty>;
  }

  return (
    <>
      <Block hint="全局给这个渠道兜底，节点上配的会盖住全局" title="改谁的参数">
        <Panel>
          <Segmented label="参数作用域" disabled={busy} choices={scopeChoices} onPick={value => { if (value !== scope && confirmNavigation()) setScope(value); }} value={scope} />
          {scope === GLOBAL ? (
            <>
              <p className="mb-1.5 text-xs text-[var(--duties-secondary)]">渠道</p>
              <Segmented label="参数格式" disabled={busy} choices={providerChoices} onPick={value => { if (value !== selected && confirmDiscard()) { setEdits({}); setProvider(value); } }} value={selected} />
            </>
          ) : (
            <Facts>
              这个节点走 <code>{scopeProvider}</code>，请求格式 <code>{selected}</code>
              {scopeProvider ? '' : '（它自己没指定渠道，跟随全局在用的那个）'}。
            </Facts>
          )}
        </Panel>
      </Block>

      <Block hint="留空表示不发这一项，由对面的默认值决定" title="参数">
        {loading ? <Empty>正在读取当前参数…</Empty> : specs.length === 0 ? (
          <Empty>这个渠道没有可调参数。</Empty>
        ) : (
          <Panel>
            <fieldset disabled={busy || !raw} className="min-w-0 space-y-3">
            {visible.map((spec) => (
              <OptionRow
                key={spec.key}
                onChange={(next) => setEdits((prev) => ({ ...prev, [spec.key]: next }))}
                spec={spec}
                value={shownOf(spec.key)}
              />
            ))}
            </fieldset>
          </Panel>
        )}
        <SaveBar
          busy={busy}
          dirty={dirty && Boolean(raw) && !loading}
          label="保存参数"
          note={note}
          onReset={() => { if (confirmDiscard()) setEdits({}); }}
          onSave={() => void save()}
        />
      </Block>

      <FallbackChain
        key={`${token}\0${scope}`}
        catalog={catalog}
        data={providers}
        onSaved={setProviders}
        scope={scope === GLOBAL ? null : scope}
      />

      <Footnote>
        参数发错会被对面直接拒掉，各家还互相不通用 —— 思考模式尤其如此，同一家的新旧模型认的写法都不一样。
        真被拒时 bot 会按对面报的错自动改一次再试，并在日志里写清楚改了什么；那条日志就是提示你回来把这里改对。
        改动写的是 runtime.yaml 和节点文件，不是 qq.yaml，所以顶部那条状态栏不会亮。
      </Footnote>
    </>
  );
};
