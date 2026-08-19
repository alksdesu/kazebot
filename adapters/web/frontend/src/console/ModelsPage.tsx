// 模型参数：各家 provider 的可调项。清单由 provider 类自己声明、后端公布，这一页只渲染和写回。
// 全局写 runtime.yaml 的 providers.<类型>.options，节点写节点文件的 provider_options，节点优先。
import { useEffect, useMemo, useState } from 'react';

import {
  getNodeRaw,
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
import { Block, Empty, Footnote, SaveBar } from './components';
import { FallbackChain } from './FallbackChain';
import { NodeYamlShapeError, readYamlScalar, upsertYamlNested } from './nodeYaml';
import { OptionRow, toYamlScalar, visibleSpecs } from './optionFields';

const GLOBAL = '__global__';

const say = (error: unknown): string => (error instanceof Error ? error.message : String(error));

interface NodeChoice {
  id: string;
  provider: string;
}

export const ModelsPage = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [catalog, setCatalog] = useState<ProviderOptionsCatalog>({});
  const [providers, setProviders] = useState<ProvidersResponse | null>(null);
  const [nodes, setNodes] = useState<NodeChoice[]>([]);
  const [scope, setScope] = useState<string>(GLOBAL);
  const [provider, setProvider] = useState('');
  const [raw, setRaw] = useState('');
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
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
    setEdits({});
    void (async () => {
      try {
        setRaw(scope === GLOBAL ? await getRuntimeRaw(token) : await getNodeRaw(token, scope));
        setNote('');
      } catch (error) {
        setRaw('');
        setNote(say(error));
      }
    })();
  }, [token, scope]);

  const providerNames = useMemo(() => Object.keys(catalog).sort(), [catalog]);
  const activeProvider = providers?.active_provider || 'openai';

  // 节点没写 provider 就跟随全局活跃渠道，和 engine 的解析顺序一致。
  const scopeProvider = useMemo(() => {
    if (scope === GLOBAL) return '';
    const hit = nodes.find((item) => item.id === scope);
    return hit?.provider || readYamlScalar(raw, ['provider']) || activeProvider;
  }, [scope, nodes, raw, activeProvider]);

  const selected = scope === GLOBAL
    ? (provider || activeProvider)
    : (scopeProvider || activeProvider);
  const specs = catalog[selected] || [];

  const pathFor = (key: string): string[] => (
    scope === GLOBAL ? ['providers', selected, 'options', key] : ['provider_options', key]
  );

  const stored = (key: string): string => (raw ? readYamlScalar(raw, pathFor(key)) : '');
  const shownOf = (key: string): string => (key in edits ? edits[key] : stored(key));

  const visible = visibleSpecs(specs, shownOf);

  const dirty = Object.entries(edits).some(([key, value]) => value !== stored(key));

  const save = async () => {
    if (!token || !raw) return;
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
        <div className="qc-panel">
          <div className="qc-grants" role="group">
            <button
              aria-pressed={scope === GLOBAL}
              className={`qc-grant${scope === GLOBAL ? ' qc-grant-on' : ''}`}
              onClick={() => setScope(GLOBAL)}
              type="button"
            >
              全局
            </button>
            {nodes.map((item) => (
              <button
                aria-pressed={scope === item.id}
                className={`qc-grant${scope === item.id ? ' qc-grant-on' : ''}`}
                key={item.id}
                onClick={() => setScope(item.id)}
                type="button"
              >
                {item.id}
              </button>
            ))}
          </div>
          {scope === GLOBAL ? (
            <>
              <p className="qc-words-label">渠道</p>
              <div className="qc-grants" role="group">
                {providerNames.map((name) => (
                  <button
                    aria-pressed={selected === name}
                    className={`qc-grant${selected === name ? ' qc-grant-on' : ''}`}
                    key={name}
                    onClick={() => setProvider(name)}
                    type="button"
                  >
                    {name}
                    {name === activeProvider && ' ·在用'}
                  </button>
                ))}
              </div>
            </>
          ) : (
            <p className="qc-facts">
              这个节点走 <code>{selected}</code>
              {scopeProvider ? '' : '（它自己没指定渠道，跟随全局在用的那个）'}。
            </p>
          )}
        </div>
      </Block>

      <Block hint="留空表示不发这一项，由对面的默认值决定" title="参数">
        {specs.length === 0 ? (
          <Empty>这个渠道没有可调参数。</Empty>
        ) : (
          <div className="qc-panel">
            {visible.map((spec) => (
              <OptionRow
                key={spec.key}
                onChange={(next) => setEdits((prev) => ({ ...prev, [spec.key]: next }))}
                spec={spec}
                value={shownOf(spec.key)}
              />
            ))}
          </div>
        )}
        <SaveBar
          busy={busy}
          dirty={dirty}
          label="保存参数"
          note={note}
          onReset={() => setEdits({})}
          onSave={() => void save()}
        />
      </Block>

      <FallbackChain
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
