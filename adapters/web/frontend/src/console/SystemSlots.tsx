// 系统槽位：压缩、摘要、接话意愿、读图、两个生图工具，各自可以走独立渠道。
//
// 留空表示跟随主渠道。工具那三个（读图 / 两个生图）由独立子进程解析，请求格式写死在
// 工具源码里，所以不给它们 provider 选项 —— 配了也不生效。
import { useEffect, useMemo, useState } from 'react';

import {
  getSystemModels,
  updateImageDefaultChannel,
  updateSystemModel,
  type ImageToolStatus,
  type ProviderProfiles,
  type SystemModelSlot,
  type SystemModelsResponse,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { EnvHint, FieldRow, HostMismatchHint, ModelField } from './channelFields';
import { Block, Empty, SaveBar } from './components';

interface Draft {
  slot: SystemModelSlot;
  model: string;
  baseUrl: string;
  provider: string;
  apiKeyInput: string;
}

const say = (error: unknown): string => (error instanceof Error ? error.message : String(error));

const toDraft = (slot: SystemModelSlot): Draft => ({
  slot,
  // 回填展开前的原文，否则保存一次就把变量引用烧成了当时的展开值。
  model: slot.model_raw,
  baseUrl: slot.base_url_raw,
  provider: slot.provider,
  apiKeyInput: '',
});

const changed = (draft: Draft): boolean => (
  draft.model !== draft.slot.model_raw
  || draft.baseUrl !== draft.slot.base_url_raw
  || draft.provider !== draft.slot.provider
  || draft.apiKeyInput.trim() !== ''
);

const Row = ({
  draft, providerNames, profiles, activeProvider, onChange,
}: {
  draft: Draft;
  providerNames: string[];
  profiles: ProviderProfiles | null;
  /** 槽位没自选渠道时落到哪家 —— 拉模型要按那一家的格式问。 */
  activeProvider: string;
  onChange: (next: Draft) => void;
}) => {
  const { slot } = draft;
  return (
    <li className="qc-cap">
      <div className="qc-cap-head">
        <span className="qc-cap-name">{slot.label}</span>
        <code className="qc-cap-scope">{slot.key}</code>
      </div>
      <p className="qc-cap-desc">{slot.desc}</p>

      {slot.supports_provider ? (
        <ModelField
          apiKey={draft.apiKeyInput}
          ariaLabel={slot.label + ' 模型'}
          baseUrl={draft.baseUrl}
          choices={[]}
          listId={'slot-' + slot.key}
          provider={draft.provider || activeProvider}
          value={draft.model}
          onChange={(model) => onChange({ ...draft, model })}
        />
      ) : (
        // 这几个槽位由工具进程直接请求，格式写死在工具源码里，没有可问的列模型接口。
        <FieldRow label="模型">
          <input
            aria-label={slot.label + ' 模型'}
            className="qc-inp"
            onChange={(event) => onChange({ ...draft, model: event.target.value })}
            placeholder="留空 = 跟随主渠道"
            value={draft.model}
          />
        </FieldRow>
      )}
      <EnvHint raw={draft.model} resolved={slot.model} savedRaw={slot.model_raw} />

      <FieldRow label="地址">
        <input
          aria-label={slot.label + ' 地址'}
          className="qc-inp"
          onChange={(event) => onChange({ ...draft, baseUrl: event.target.value })}
          placeholder="留空 = 跟随主渠道"
          value={draft.baseUrl}
        />
      </FieldRow>
      <EnvHint raw={draft.baseUrl} resolved={slot.base_url} savedRaw={slot.base_url_raw} />

      <FieldRow label="密钥">
        <input
          aria-label={slot.label + ' 密钥'}
          className="qc-inp"
          onChange={(event) => onChange({ ...draft, apiKeyInput: event.target.value })}
          placeholder={slot.api_key_present ? '已设置 ' + slot.api_key_redacted + '，留空不改' : '跟随主渠道'}
          type="password"
          value={draft.apiKeyInput}
        />
      </FieldRow>

      {slot.supports_provider && (
        <FieldRow label="渠道">
          <select
            aria-label={slot.label + ' 渠道'}
            className="qc-inp"
            onChange={(event) => onChange({ ...draft, provider: event.target.value })}
            value={draft.provider}
          >
            <option value="">跟随主渠道</option>
            {providerNames.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </FieldRow>
      )}

      {!slot.supports_provider && draft.baseUrl && (
        <p className="qc-cap-desc">
          这一项由工具进程直接请求，格式固定，换成别家的地址会失败。
          {/* Gemini 那栏会自己剥掉 /v1，只有这里少写一段就静默 404。 */}
          {slot.key === 'image_gpt' && '地址要写到 /v1 为止。'}
        </p>
      )}
      {slot.supports_provider && draft.baseUrl && !draft.provider && (
        <p className="qc-cap-desc">换家要连渠道一起选，只改地址会按主渠道的格式发出去。</p>
      )}
      {slot.supports_provider && (
        <HostMismatchHint baseUrl={draft.baseUrl} profiles={profiles} provider={draft.provider} />
      )}
    </li>
  );
};

/** 只在真有得选时出现：一个渠道时没什么可选，一个都没有时该去填 key 而不是选默认。 */
const ImageDefault = ({ tools, value, onPick }: {
  tools: ImageToolStatus[];
  value: string;
  onPick: (next: string) => void;
}) => {
  const live = tools.filter((tool) => tool.available);
  if (live.length < 2) return null;
  return (
    <li className="qc-cap">
      <div className="qc-cap-head">
        <span className="qc-cap-name">默认生图渠道</span>
      </div>
      <p className="qc-cap-desc">两个渠道都配好了。模型拿不准用哪个时走这里选的那个。</p>
      <FieldRow label="默认用">
        <select
          aria-label="默认生图渠道"
          className="qc-inp"
          onChange={(event) => onPick(event.target.value)}
          value={value}
        >
          <option value="">按用途自动判断</option>
          {live.map((tool) => <option key={tool.name} value={tool.name}>{tool.name}</option>)}
        </select>
      </FieldRow>
    </li>
  );
};

export const SystemSlots = ({ providerNames, profiles, activeProvider }: {
  providerNames: string[];
  profiles: ProviderProfiles | null;
  activeProvider: string;
}) => {
  const token = useSettingsStore((state) => state.adminToken);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [imageTools, setImageTools] = useState<ImageToolStatus[]>([]);
  const [imageDefault, setImageDefault] = useState('');
  const [savedImageDefault, setSavedImageDefault] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');

  const absorb = (data: SystemModelsResponse) => {
    setDrafts(data.slots.map(toDraft));
    setImageTools(data.image_tools ?? []);
    setImageDefault(data.image_default_channel ?? '');
    setSavedImageDefault(data.image_default_channel ?? '');
    setLoaded(true);
  };

  useEffect(() => {
    if (!token) return;
    void getSystemModels(token).then(absorb).catch((caught) => {
      setError(say(caught));
      setLoaded(true);
    });
  }, [token]);

  const dirty = useMemo(
    () => drafts.some(changed) || imageDefault !== savedImageDefault,
    [drafts, imageDefault, savedImageDefault],
  );

  const save = async () => {
    if (!token) return;
    setBusy(true);
    setError('');
    try {
      let latest: SystemModelsResponse | null = null;
      for (const draft of drafts) {
        if (!changed(draft)) continue;
        const key = draft.apiKeyInput.trim();
        latest = await updateSystemModel(token, draft.slot.key, {
          // 空串是「删掉这一项」的信号，和「不传」不是一回事。
          model: draft.model.trim(),
          base_url: draft.baseUrl.trim(),
          provider: draft.slot.supports_provider ? draft.provider : '',
          ...(key ? { api_key: key } : {}),
        });
      }
      // 放在槽位之后：改完 key 才知道现在到底有几个渠道可选。
      if (imageDefault !== savedImageDefault) {
        latest = await updateImageDefaultChannel(token, imageDefault);
      }
      if (latest) {
        absorb(latest);
        setNote('已保存。下一个任务就用新配置。');
      }
    } catch (caught) {
      setError(say(caught));
    }
    setBusy(false);
  };

  if (!loaded) return <Empty>正在读取系统槽位…</Empty>;

  return (
    <Block hint="压缩、摘要、读图这些内部用途各自可以走独立渠道，留空则跟随主渠道" title="系统槽位">
      {drafts.length === 0 ? (
        <Empty>没有可配的槽位。</Empty>
      ) : (
        <ul className="qc-caps">
          {drafts.map((draft, index) => (
            <Row
              draft={draft}
              key={draft.slot.key}
              onChange={(next) => setDrafts(drafts.map((item, i) => (i === index ? next : item)))}
              activeProvider={activeProvider}
              profiles={profiles}
              providerNames={providerNames}
            />
          ))}
          <ImageDefault onPick={setImageDefault} tools={imageTools} value={imageDefault} />
        </ul>
      )}
      {error && <p className="qc-login-error">{error}</p>}
      <SaveBar
        busy={busy}
        dirty={dirty}
        label="保存槽位"
        note={note}
        onReset={() => {
          setDrafts(drafts.map((draft) => toDraft(draft.slot)));
          setImageDefault(savedImageDefault);
        }}
        onSave={() => void save()}
      />
    </Block>
  );
};
