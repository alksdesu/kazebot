// 看图渠道。存在 data/config.yaml 的 system_models.image，不是 qq.yaml，所以自己存盘。
import { useEffect, useState } from 'react';

import {
  getProviderProfiles,
  getProviders,
  getSystemModels,
  updateSystemModel,
  type ProviderProfiles,
  type SystemModelSlot,
  type SystemModelsResponse,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { EnvHint, FieldRow, HostMismatchHint, ModelField } from './channelFields';
import { Block, Empty, SaveBar } from './components';

const say = (error: unknown): string => (error instanceof Error ? error.message : '出错了');

export const VisionChannelBlock = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const [slot, setSlot] = useState<SystemModelSlot | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [provider, setProvider] = useState('');
  const [providerNames, setProviderNames] = useState<string[]>([]);
  const [profiles, setProfiles] = useState<ProviderProfiles | null>(null);
  const [mainProvider, setMainProvider] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');

  const absorb = (data: SystemModelsResponse) => {
    const found = data.slots.find((item) => item.key === 'image') || null;
    setSlot(found);
    // 回填展开前的原文，否则保存一次就把变量引用烧成了当时的展开值。
    setBaseUrl(found?.base_url_raw || '');
    setModel(found?.model_raw || '');
    setProvider(found?.provider || '');
    setApiKey('');
  };

  useEffect(() => {
    if (!adminToken) return;
    void (async () => {
      try {
        absorb(await getSystemModels(adminToken));
      } catch (caught) { setError(say(caught)); }
      try {
        const [providers, catalog] = await Promise.all([
          getProviders(adminToken), getProviderProfiles(adminToken),
        ]);
        setMainProvider(providers.active_provider);
        setProviderNames(providers.registered || []);
        setProfiles(catalog);
      } catch {
        // 这两份只用来渲染渠道下拉和对不上的提示，取不到不该挡住保存。
      }
      setLoaded(true);
    })();
  }, [adminToken]);

  const run = async (job: (token: string) => Promise<string>) => {
    if (!adminToken || busy) return;
    setBusy(true);
    setError('');
    setNote('');
    try { setNote(await job(adminToken)); } catch (caught) { setError(say(caught)); }
    setBusy(false);
  };

  const dirty = slot !== null && (
    baseUrl !== slot.base_url_raw || model !== slot.model_raw
    || provider !== slot.provider || apiKey.trim() !== ''
  );

  const save = () => void run(async (token) => {
    const key = apiKey.trim();
    absorb(await updateSystemModel(token, 'image', {
      base_url: baseUrl.trim(),
      model: model.trim(),
      provider,
      // 只在真敲了东西时才带上 api_key：空串是「删掉这一项」的信号，不是「不动」。
      ...(key ? { api_key: key } : {}),
    }));
    return '已保存。下一轮打标就用新配置。';
  });

  const clearKey = () => {
    if (!window.confirm('清除看图渠道的密钥？清掉之后这一项回到跟随主渠道，存的那把密钥找不回来。')) return;
    void run(async (token) => {
      absorb(await updateSystemModel(token, 'image', { api_key: '' }));
      return '已清除密钥';
    });
  };

  const reset = () => {
    setBaseUrl(slot?.base_url_raw || '');
    setModel(slot?.model_raw || '');
    setProvider(slot?.provider || '');
    setApiKey('');
  };

  // 自己指了地址却没说是哪家，就按 OpenAI 格式发。中转站多半没问题，换家必挂。
  const wireUnstated = !!baseUrl.trim() && !provider;
  const effective = provider || (baseUrl.trim() ? 'openai' : mainProvider);

  return (
    <Block hint="打标和 read_image 工具共用这一条，改完点这一块自己的保存" title="看图渠道">
      {!loaded ? <Empty>正在读取看图渠道…</Empty> : (
        <div className="qc-panel">
          <p className="qc-facts">
            {slot?.desc || '给看不了图的模型描述图片内容。'}
            全部留空则整条跟随主渠道，连请求格式一起跟。
          </p>

          <FieldRow label="渠道">
            <select
              aria-label="看图渠道类型"
              className="qc-inp"
              onChange={(event) => setProvider(event.target.value)}
              value={provider}
            >
              <option value="">跟随主渠道{mainProvider ? `（${mainProvider}）` : ''}</option>
              {providerNames.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </FieldRow>
          <p className="qc-facts">
            决定按谁的格式发请求。地址填到哪一级也看它：OpenAI 系要带 <code>/v1</code>，
            Claude 与 Gemini 不带。
          </p>
          {wireUnstated && (
            <p className="qc-mismatch">
              填了地址却没选渠道，请求按 OpenAI 格式发。中转站一般没问题，
              直连 Claude 或 Gemini 的话这里要一起选。
            </p>
          )}

          <FieldRow label="地址">
            <input
              aria-label="看图渠道地址"
              className="qc-inp"
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder="留空 = 跟随主渠道的地址"
              value={baseUrl}
            />
          </FieldRow>
          <EnvHint raw={baseUrl} resolved={slot?.base_url || ''} savedRaw={slot?.base_url_raw || ''} />
          <HostMismatchHint baseUrl={baseUrl} profiles={profiles} provider={provider} />

          <ModelField
            apiKey={apiKey}
            ariaLabel="看图渠道模型"
            baseUrl={baseUrl}
            choices={[]}
            listId="sticker-vision-models"
            provider={effective}
            slot="image"
            value={model}
            onChange={setModel}
          />
          <EnvHint raw={model} resolved={slot?.model || ''} savedRaw={slot?.model_raw || ''} />
          <p className="qc-facts">模型名不跟随主渠道 —— 主渠道那个多半正是看不了图的纯文本模型。</p>

          <FieldRow label="密钥">
            <input
              aria-label="看图渠道密钥"
              className="qc-inp"
              onChange={(event) => setApiKey(event.target.value)}
              placeholder={slot?.api_key_present
                ? `已设置 ${slot.api_key_redacted}，留空不改`
                : '留空 = 跟随主渠道的密钥'}
              type="password"
              value={apiKey}
            />
            {slot?.api_key_present && (
              <button className="qc-btn qc-btn-danger" disabled={busy} onClick={clearKey} type="button">
                清除
              </button>
            )}
          </FieldRow>
          <p className="qc-facts">
            密钥只存不回显。
            {slot?.api_key_present
              ? '这里留空表示沿用已存的那一把，不会清掉；真要清就点上面的「清除」。'
              : '这一项还没配。'}
          </p>

          {error && <p className="qc-login-error">{error}</p>}
          <SaveBar busy={busy} dirty={dirty} label="保存渠道" note={note} onReset={reset} onSave={save} />
        </div>
      )}
    </Block>
  );
};

