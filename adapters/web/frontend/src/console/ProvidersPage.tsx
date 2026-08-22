// 渠道：bot 实际调用哪一家。key/地址/模型都在这里，参数和备选链在「模型」页。
//
// 读到的是脱敏视图，密钥只回来一个星号串，所以留空表示不改而不是清空。
// 新建的渠道先留在本地，存盘时才建块 —— 后端认不出空块，先建会让它从列表里消失。
import { useEffect, useId, useMemo, useState } from 'react';

import {
  deleteProvider,
  getNodes,
  getProviders,
  getProviderProfiles,
  setActiveProvider,
  upsertProvider,
  type ProviderConfigPublic,
  type ProviderProfiles,
  type ProvidersResponse,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { mergeModelChoices, modelsFromProviders } from '../utils/modelChoices';
import { EnvHint, FieldRow, HostMismatchHint, ModelField } from './channelFields';
import { Block, Empty, Footnote, SaveBar } from './components';
import { SystemSlots } from './SystemSlots';
import { VisionRouting } from './VisionRouting';

interface Draft {
  name: string;
  /** 线格式。决定请求按谁的格式发，和渠道名再无关系。 */
  type: string;
  /** 块里真写了 type，而不是从名字猜出来的。 */
  typeExplicit: boolean;
  /** 给人看的名字，留空就显示块名。 */
  label: string;
  model: string;
  baseUrl: string;
  apiKeyInput: string;
  keyPresent: boolean;
  keyRedacted: string;
  /** 收不收图片。auto = 跟随这家 provider 的默认。 */
  vision: 'auto' | 'yes' | 'no';
  /** 服务端展开后的值，只用来提示，不回填。 */
  modelResolved: string;
  baseUrlResolved: string;
  /** 还没在 config.yaml 里建块。 */
  fresh: boolean;
}

const say = (error: unknown): string => (error instanceof Error ? error.message : String(error));

const toDraft = (name: string, block: ProviderConfigPublic): Draft => ({
  name,
  type: block.type || name,
  typeExplicit: !!block.type_explicit,
  label: block.label || '',
  // 回填展开前的原文，否则保存一次就把变量引用烧成了当时的展开值。
  model: block.model_raw,
  baseUrl: block.base_url_raw,
  apiKeyInput: '',
  keyPresent: block.api_key_present,
  keyRedacted: block.api_key_redacted,
  modelResolved: block.model,
  baseUrlResolved: block.base_url,
  // null 是「没配」，和显式关掉不是一回事。
  vision: block.supports_vision === null || block.supports_vision === undefined
    ? 'auto'
    : (block.supports_vision ? 'yes' : 'no'),
  fresh: false,
});

const fromResponse = (data: ProvidersResponse): Draft[] => Object.entries(data.providers)
  .map(([name, block]) => toDraft(name, block))
  .sort((a, b) => a.name.localeCompare(b.name));

const changed = (draft: Draft, stored: Draft | undefined): boolean => (
  draft.fresh
  || !stored
  || draft.model !== stored.model
  || draft.baseUrl !== stored.baseUrl
  || draft.vision !== stored.vision
  || draft.type !== stored.type
  || draft.label !== stored.label
  || draft.apiKeyInput.trim() !== ''
);

const Row = ({
  draft, stored, active, busy, choices, listId, profiles, families, onChange, onActivate, onRemove,
}: {
  draft: Draft;
  stored: Draft | undefined;
  active: boolean;
  busy: boolean;
  profiles: ProviderProfiles | null;
  /** 别的渠道和备选链在用的模型名。上游中转定义值空间，这只是提示，不是封闭集合。 */
  choices: string[];
  /** 后端注册过的线格式。 */
  families: string[];
  listId: string;
  onChange: (next: Draft) => void;
  onActivate: () => void;
  onRemove: () => void;
}) => (
  <li className="qc-cap">
    <div className="qc-cap-head">
      <span className="qc-cap-name">{draft.label || draft.name}</span>
      {draft.label && <span className="qc-cap-scope">{draft.name}</span>}
      {active
        ? <span className="qc-cap-scope">在用</span>
        : (
          <button
            className="qc-btn qc-btn-quiet"
            disabled={busy || draft.fresh}
            onClick={onActivate}
            title={draft.fresh ? '先保存这个渠道，再设为活跃' : undefined}
            type="button"
          >
            设为活跃
          </button>
        )}
      <span className="qc-bar-spacer" />
      <button
        className="qc-btn qc-btn-halt"
        disabled={busy || active}
        onClick={onRemove}
        title={active ? '正在用的渠道不能删' : undefined}
        type="button"
      >
        删除
      </button>
    </div>

    <FieldRow label="格式">
      <select
        aria-label={draft.name + ' 线格式'}
        className="qc-inp"
        onChange={(event) => onChange({ ...draft, type: event.target.value, typeExplicit: true })}
        value={draft.type}
      >
        {!families.includes(draft.type) && <option value={draft.type}>{draft.type}</option>}
        {families.map((family) => <option key={family} value={family}>{family}</option>)}
      </select>
    </FieldRow>
    {!draft.typeExplicit && !draft.fresh && (
      <p className="qc-facts">没写 type，按渠道名当成了 {draft.type}。改这一项会把它明确写进配置。</p>
    )}

    <FieldRow label="备注">
      <input
        aria-label={draft.name + ' 备注'}
        className="qc-inp"
        onChange={(event) => onChange({ ...draft, label: event.target.value })}
        placeholder="给自己看的名字，留空就显示渠道名"
        value={draft.label}
      />
    </FieldRow>

    <ModelField
      apiKey={draft.apiKeyInput}
      ariaLabel={draft.name + ' 模型'}
      baseUrl={draft.baseUrl}
      choices={choices}
      listId={listId}
      provider={draft.name}
      value={draft.model}
      onChange={(model) => onChange({ ...draft, model })}
    />
    <EnvHint raw={draft.model} resolved={draft.modelResolved} savedRaw={stored?.model ?? ''} />

    <FieldRow label="地址">
      <input
        aria-label={draft.name + ' 地址'}
        className="qc-inp"
        onChange={(event) => onChange({ ...draft, baseUrl: event.target.value })}
        placeholder="留空用这家的默认地址"
        value={draft.baseUrl}
      />
    </FieldRow>
    <EnvHint raw={draft.baseUrl} resolved={draft.baseUrlResolved} savedRaw={stored?.baseUrl ?? ''} />
    <HostMismatchHint baseUrl={draft.baseUrl} profiles={profiles} provider={draft.type} />

    <FieldRow label="密钥">
      <input
        aria-label={draft.name + ' 密钥'}
        className="qc-inp"
        onChange={(event) => onChange({ ...draft, apiKeyInput: event.target.value })}
        placeholder={draft.keyPresent ? '已设置 ' + draft.keyRedacted + '，留空不改' : '未设置'}
        type="password"
        value={draft.apiKeyInput}
      />
    </FieldRow>

    <FieldRow label="带图">
      <select
        aria-label={draft.name + ' 读图能力'}
        className="qc-inp"
        id={listId + '-vision'}
        onChange={(event) => onChange({ ...draft, vision: event.target.value as Draft['vision'] })}
        value={draft.vision}
      >
        <option value="auto">
          {'跟随 ' + draft.type + ' 默认（' + (profiles?.defaultVision?.[draft.type] === false ? '看不了图' : '能看图') + '）'}
        </option>
        <option value="yes">能看图</option>
        <option value="no">看不了图</option>
      </select>
    </FieldRow>
  </li>
);

/** 写死了自己渠道的节点不会跟随全局，切换前得让人知道。 */
const NodeOverrides = ({ nodes }: { nodes: Array<{ id: string; provider: string }> }) => {
  if (!nodes.length) return null;
  return (
    <p className="qc-facts">
      这 {nodes.length} 个节点写死了自己的渠道，切换活跃渠道不影响它们：
      {nodes.map((node) => node.id + '（' + node.provider + '）').join('、')}。
      要改去「设置 → 节点文件」。
    </p>
  );
};

export const ProvidersPage = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const listPrefix = useId();
  const [data, setData] = useState<ProvidersResponse | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [pinned, setPinned] = useState<Draft[]>([]);
  const [overrides, setOverrides] = useState<Array<{ id: string; provider: string }>>([]);
  const [profiles, setProfiles] = useState<ProviderProfiles | null>(null);
  const [addName, setAddName] = useState('');
  const [addType, setAddType] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    if (!token) return;
    void (async () => {
      try {
        const resp = await getProviders(token);
        setData(resp);
        setDrafts(fromResponse(resp));
        setPinned(fromResponse(resp));
      } catch (caught) {
        setError(say(caught));
      }
      try {
        const list = await getNodes(token);
        setOverrides(
          list
            .map((node) => ({ id: String(node.id || ''), provider: String(node.provider || '') }))
            .filter((node) => node.id && node.provider),
        );
      } catch {
        // 提示性信息，取不到就不提示，不该挡住这一页。
      }
      try {
        setProfiles(await getProviderProfiles(token));
      } catch {
        // 同上：认不出域名就是不提示。
      }
    })();
  }, [token]);

  const storedBy = useMemo(
    () => new Map(pinned.map((draft) => [draft.name, draft])),
    [pinned],
  );
  const dirty = drafts.some((draft) => changed(draft, storedBy.get(draft.name)));

  const families = data?.registered || [];
  // 名字留空就拿格式名当名字：同一家只开一个时，这和以前的行为一模一样。
  const pendingName = addName.trim() || addType;
  const nameTaken = drafts.some((draft) => draft.name === pendingName);

  const absorb = (next: ProvidersResponse, done: string) => {
    setData(next);
    setDrafts(fromResponse(next));
    setPinned(fromResponse(next));
    setNote(done);
  };

  const run = async (action: (auth: string) => Promise<ProvidersResponse>, done: string) => {
    if (!token) return;
    setBusy(true);
    setError('');
    try {
      absorb(await action(token), done);
    } catch (caught) {
      setError(say(caught));
    }
    setBusy(false);
  };

  const save = async () => {
    if (!token) return;
    setBusy(true);
    setError('');
    try {
      let latest = data;
      for (const draft of drafts) {
        if (!changed(draft, storedBy.get(draft.name))) continue;
        const key = draft.apiKeyInput.trim();
        latest = await upsertProvider(token, draft.name, {
          model: draft.model.trim(),
          base_url: draft.baseUrl.trim(),
          supports_vision: draft.vision,
          label: draft.label.trim(),
          // 名字猜不出格式时必须写死，否则后端只能按块名去查，查不到就拒。
          ...(draft.typeExplicit || draft.type !== draft.name ? { type: draft.type } : {}),
          // 只有真填了才提交，留空表示沿用原值。
          ...(key ? { api_key: key } : {}),
        });
      }
      if (latest) absorb(latest, '已保存。engine 每个任务重新取一次密钥，下一条消息就用新的。');
    } catch (caught) {
      setError(say(caught));
    }
    setBusy(false);
  };

  if (!data) return <Empty>{error || '正在读取渠道…'}</Empty>;

  return (
    <>
      <Block hint="bot 实际调用的那一家。节点没单独指定就都走活跃渠道" title="渠道">
        {drafts.length === 0 ? (
          <Empty>还没有配置任何渠道。</Empty>
        ) : (
          <ul className="qc-caps">
            {drafts.map((draft, index) => (
              <Row
                active={draft.name === data.active_provider}
                busy={busy}
                choices={mergeModelChoices(modelsFromProviders(data, draft.name))}
                draft={draft}
                families={families}
                key={draft.name}
                listId={listPrefix + '-' + draft.name}
                profiles={profiles}
                stored={storedBy.get(draft.name)}
                onActivate={() => void run(
                  (auth) => setActiveProvider(auth, draft.name),
                  '已切到 ' + draft.name + '。',
                )}
                onChange={(next) => setDrafts(drafts.map((item, i) => (i === index ? next : item)))}
                onRemove={() => {
                  if (draft.fresh) {
                    setDrafts(drafts.filter((_, i) => i !== index));
                    return;
                  }
                  void run((auth) => deleteProvider(auth, draft.name), '已删除 ' + draft.name + '。');
                }}
              />
            ))}
          </ul>
        )}

        {families.length > 0 && (
          <>
            <div className="qc-cap-head">
              <input
                aria-label="新渠道名"
                className="qc-inp"
                onChange={(event) => setAddName(event.target.value)}
                placeholder="渠道名，留空就用格式名"
                value={addName}
              />
              <select
                aria-label="新渠道格式"
                className="qc-inp"
                onChange={(event) => setAddType(event.target.value)}
                value={addType}
              >
                <option value="">选格式…</option>
                {families.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
              <button
                className="qc-btn qc-btn-quiet"
                disabled={!addType || nameTaken}
                onClick={() => {
                  setDrafts([...drafts, {
                    name: pendingName,
                    type: addType,
                    typeExplicit: true,
                    label: '',
                    model: '',
                    baseUrl: '',
                    apiKeyInput: '',
                    keyPresent: false,
                    keyRedacted: '',
                    modelResolved: '',
                    baseUrlResolved: '',
                    vision: 'auto',
                    fresh: true,
                  }]);
                  setAddName('');
                  setAddType('');
                }}
                type="button"
              >
                添加
              </button>
            </div>
            {nameTaken && <p className="qc-facts">已经有一个叫 {pendingName} 的渠道了，换个名字。</p>}
          </>
        )}

        <NodeOverrides nodes={overrides} />
        {error && <p className="qc-login-error">{error}</p>}
        <SaveBar
          busy={busy}
          dirty={dirty}
          label="保存渠道"
          note={note}
          onReset={() => setDrafts(pinned.map((draft) => ({ ...draft })))}
          onSave={() => void save()}
        />
      </Block>

      <VisionRouting />

      <SystemSlots
        activeProvider={data.active_provider}
        profiles={profiles}
        providerNames={data.registered || []}
      />

      <Footnote>
        密钥可以写成环境变量引用，只把变量名留在 config.yaml 里，值放 .env。地址留空时用这家的默认端点。
        删掉的渠道如果还被备选链引用，那条链会在轮到它时失败 —— 去「模型」页一并清掉。
      </Footnote>
    </>
  );
};
