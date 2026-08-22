// 备选链：主渠道失败时按顺序往下试。全局一条，节点可以有自己的一条。
//
// 界面读到的是脱敏视图 —— 内联密钥只回来一个星号串，手写进 yaml 的键根本看不见。
// 所以每条都带上原始位置提交，由后端按条目合并；留空的密钥表示不改，不是清空。
import { useEffect, useMemo, useState } from 'react';

import {
  channelChoices,
  clearNodeFallbacks,
  updateFallbacks,
  updateNodeFallbacks,
  type ChannelChoice,
  type FallbackEntryPublic,
  type FallbackEntryUpdate,
  type ProviderOptionsCatalog,
  type ProvidersResponse,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { ChannelOptions, wireOf } from './channelFields';
import { Block, Button, Check, Desc, Empty, Facts, Input, Item, Panel, SaveBar, Segmented, Select } from './components';
import { OptionRow, shownFromValue, valueFromShown, visibleSpecs } from './optionFields';

/** 编辑态。api_key 单独存：空串表示不改，不是清空。 */
interface Draft extends FallbackEntryPublic {
  origin: number | null;
  apiKeyInput: string;
}

const say = (error: unknown): string => (error instanceof Error ? error.message : String(error));

const toDraft = (entry: FallbackEntryPublic, index: number): Draft => ({
  ...entry,
  options: { ...(entry.options || {}) },
  origin: index,
  apiKeyInput: '',
});

const blankDraft = (provider: string): Draft => ({
  provider,
  model: '',
  base_url: '',
  model_raw: '',
  base_url_raw: '',
  api_key_present: false,
  api_key_redacted: '',
  supports_vision: false,
  options: {},
  origin: null,
  apiKeyInput: '',
});

function toPayload(draft: Draft): FallbackEntryUpdate {
  const entry: FallbackEntryUpdate = {
    provider: draft.provider,
    // 回填的是展开前的原文，否则保存一次就把 ${VAR} 烧成了当时的展开值。
    model: draft.model_raw.trim() || null,
    base_url: draft.base_url_raw.trim() || null,
    supports_vision: draft.supports_vision,
    options: draft.options,
  };
  if (draft.origin !== null) entry._origin = draft.origin;
  // 只有真填了才提交密钥；留空表示沿用原值。
  if (draft.apiKeyInput.trim()) entry.api_key = draft.apiKeyInput.trim();
  return entry;
}

const sameChain = (a: Draft[], b: FallbackEntryPublic[]): boolean => (
  JSON.stringify(a.map((draft) => ({ ...toPayload(draft), _origin: undefined })))
  === JSON.stringify(b.map((entry) => ({ ...toPayload(toDraft(entry, 0)), _origin: undefined })))
);

const HEAD = 'flex items-center gap-2';

const Row = ({
  draft, index, total, catalog, channels, wires, onChange, onMove, onRemove,
}: {
  draft: Draft;
  index: number;
  total: number;
  catalog: ProviderOptionsCatalog;
  channels: ChannelChoice[];
  wires: string[];
  onChange: (next: Draft) => void;
  onMove: (delta: number) => void;
  onRemove: () => void;
}) => {
  const [open, setOpen] = useState(false);
  // catalog 按线格式索引，拿渠道名去查什么都查不到。
  const specs = catalog[wireOf(draft.provider, channels)] || [];
  const shownOf = (key: string): string => shownFromValue(draft.options?.[key]);
  const setOption = (key: string, shown: string) => {
    const spec = specs.find((item) => item.key === key);
    if (!spec) return;
    const value = valueFromShown(spec, shown);
    const next = { ...(draft.options || {}) };
    // null 表示这一项回到「不指定」；留着 null 会被后端当成删除指令，正合适。
    if (value === null) delete next[key];
    else next[key] = value;
    onChange({ ...draft, options: next });
  };
  const tuned = Object.keys(draft.options || {}).length;

  return (
    <Item>
      <div className={HEAD}>
        <span className="font-medium">{index + 1}</span>
        <Select
          aria-label="渠道"
          onChange={(event) => onChange({ ...draft, provider: event.target.value, options: {} })}
          value={draft.provider}
        >
          <ChannelOptions channels={channels} current={draft.provider} wires={wires} />
        </Select>
        <Input
          aria-label="模型"
          onChange={(event) => onChange({ ...draft, model_raw: event.target.value, model: event.target.value })}
          placeholder="留空 = 继承同名渠道"
          value={draft.model_raw}
        />
        <span className="flex-1" />
        <Button disabled={index === 0} onClick={() => onMove(-1)} tone="quiet">上移</Button>
        <Button disabled={index === total - 1} onClick={() => onMove(1)} tone="quiet">下移</Button>
        <Button onClick={onRemove} tone="halt">删除</Button>
      </div>

      <div className={HEAD}>
        <Input
          aria-label="地址"
          onChange={(event) => onChange({ ...draft, base_url_raw: event.target.value, base_url: event.target.value })}
          placeholder="留空 = 继承同名渠道的地址"
          value={draft.base_url_raw}
          width="wide"
        />
        <Input
          aria-label="密钥"
          onChange={(event) => onChange({ ...draft, apiKeyInput: event.target.value })}
          placeholder={draft.api_key_present ? '已设置，留空不改' : '留空 = 继承同名渠道'}
          type="password"
          value={draft.apiKeyInput}
        />
        <Check
          checked={draft.supports_vision}
          onChange={(checked) => onChange({ ...draft, supports_vision: checked })}
        >
          支持图片
        </Check>
        <span className="flex-1" />
        <Button onClick={() => setOpen(!open)} tone="quiet">
          参数{tuned ? ` · ${tuned}` : ''}
        </Button>
      </div>

      {open && (
        specs.length === 0
          ? <Facts>这个渠道没有可调参数。</Facts>
          : (
            <Panel>
              {visibleSpecs(specs, shownOf).map((spec) => (
                <OptionRow
                  key={spec.key}
                  onChange={(next) => setOption(spec.key, next)}
                  spec={spec}
                  value={shownOf(spec.key)}
                />
              ))}
            </Panel>
          )
      )}

      {!draft.supports_vision && (
        <Desc indent={false}>带图的请求会跳过这一条 —— 没勾「支持图片」就当它读不了图。</Desc>
      )}
    </Item>
  );
};

const NodeChainMode = ({
  busy, custom, disabledNow, globalCount, nodeId, onDisable, onEnable, onFollowGlobal,
}: {
  busy: boolean;
  custom: boolean;
  disabledNow: boolean;
  globalCount: number;
  nodeId: string;
  onDisable: (nodeId: string) => Promise<void>;
  onEnable: (nodeId: string) => Promise<void>;
  onFollowGlobal: (nodeId: string) => Promise<void>;
}) => {
  const modes: Array<[string, boolean, (id: string) => Promise<void>]> = [
    ['跟随全局', !custom, onFollowGlobal],
    ['自定义', custom && !disabledNow, onEnable],
    ['禁用', disabledNow, onDisable],
  ];
  return (
    <Panel>
      {/* fieldset 是为了把 busy 一次性传到三个档位上，disabled 只有原生元素继承得了。 */}
      <fieldset className={busy ? 'opacity-50' : ''} disabled={busy}>
        <Segmented
          choices={modes.map(([label]) => [label, label] as const)}
          onPick={(label) => void modes.find(([name]) => name === label)?.[2](nodeId)}
          value={modes.find(([, on]) => on)?.[0] ?? ''}
        />
      </fieldset>
      {!custom && (
        <Facts>
          现在跟随全局链（{globalCount} 条）。选「自定义」才会给这个节点单独配。
        </Facts>
      )}
      {disabledNow && (
        <Facts>已禁用：这个节点的调用失败后直接报错，不再试别的渠道。</Facts>
      )}
    </Panel>
  );
};

export const FallbackChain = ({
  scope, data, catalog, onSaved,
}: {
  scope: string | null;
  data: ProvidersResponse | null;
  catalog: ProviderOptionsCatalog;
  onSaved: (next: ProvidersResponse) => void;
}) => {
  const token = useSettingsStore((state) => state.adminToken);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');

  const { channels, wires } = useMemo(() => channelChoices(data), [data]);
  // 新建的一条默认指向第一个已配渠道 —— 那才是马上能用的，裸线格式还得再填地址密钥。
  const defaultProvider = channels[0]?.value || wires[0] || 'openai';
  const custom = scope === null ? true : scope in (data?.node_fallbacks || {});
  const stored = useMemo<FallbackEntryPublic[]>(() => {
    if (!data) return [];
    return scope === null ? data.fallbacks : (data.node_fallbacks?.[scope] || []);
  }, [data, scope]);

  // 换作用域或重新拉到数据时重建编辑态，草稿属于上一份。
  useEffect(() => {
    setDrafts(stored.map(toDraft));
    setNote('');
  }, [stored]);

  const dirty = !sameChain(drafts, stored);

  const update = (index: number, next: Draft) => {
    setDrafts(drafts.map((item, i) => (i === index ? next : item)));
  };
  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= drafts.length) return;
    const next = [...drafts];
    [next[index], next[target]] = [next[target], next[index]];
    setDrafts(next);
  };

  // action 收 token：外面判过空了，但闭包里 TS 不认收窄，传进来才不用到处写断言。
  const run = async (action: (auth: string) => Promise<ProvidersResponse>, done: string) => {
    if (!token) return;
    setBusy(true);
    try {
      onSaved(await action(token));
      setNote(done);
    } catch (error) {
      setNote(say(error));
    }
    setBusy(false);
  };

  const save = () => run(
    (auth) => (scope === null
      ? updateFallbacks(auth, drafts.map(toPayload))
      : updateNodeFallbacks(auth, scope, drafts.map(toPayload))),
    '已保存。下一次主渠道失败就按这条链往下试。',
  );

  if (!data) return <Empty>正在读取备选链…</Empty>;

  const hint = scope === null
    ? '主渠道失败时按顺序往下试'
    : '这个节点失败时用自己的链，而不是全局那条';

  return (
    <Block hint={hint} title="备选链">
      {scope !== null && (
        <NodeChainMode
          busy={busy}
          custom={custom}
          disabledNow={custom && stored.length === 0}
          globalCount={data.fallbacks.length}
          nodeId={scope}
          onDisable={(id) => run(
            (auth) => updateNodeFallbacks(auth, id, []),
            '已禁用。这个节点失败后不再尝试任何备选。',
          )}
          onEnable={(id) => run(
            (auth) => updateNodeFallbacks(auth, id, drafts.length
              ? drafts.map(toPayload)
              : [toPayload(blankDraft(defaultProvider))]),
            '已改为使用这个节点自己的链。',
          )}
          onFollowGlobal={(id) => run(
            (auth) => clearNodeFallbacks(auth, id), '已改为跟随全局链。',
          )}
        />
      )}

      {(scope === null || (custom && (stored.length > 0 || drafts.length > 0))) && (
        <>
          {drafts.length === 0 ? (
            <Empty>还没有备选渠道。主渠道失败时任务直接报错。</Empty>
          ) : (
            <ul className="overflow-hidden border border-[var(--duties-border)]">
              {drafts.map((draft, index) => (
                <Row
                  catalog={catalog}
                  draft={draft}
                  index={index}
                  key={`${draft.origin ?? 'new'}-${index}`}
                  onChange={(next) => update(index, next)}
                  onMove={(delta) => move(index, delta)}
                  onRemove={() => setDrafts(drafts.filter((_, i) => i !== index))}
                  channels={channels}
                  wires={wires}
                  total={drafts.length}
                />
              ))}
            </ul>
          )}
          <div className={HEAD}>
            <Button onClick={() => setDrafts([...drafts, blankDraft(defaultProvider)])} tone="quiet">
              添加备选
            </Button>
          </div>
          <SaveBar
            busy={busy}
            dirty={dirty}
            label="保存备选链"
            note={note}
            onReset={() => setDrafts(stored.map(toDraft))}
            onSave={() => void save()}
          />
        </>
      )}
    </Block>
  );
};
