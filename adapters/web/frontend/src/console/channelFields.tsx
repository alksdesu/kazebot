// 渠道编辑共用的小件。渠道页和系统槽位都摆 model/地址/密钥三项，回填规则也一样。
import { useMemo, useState, type ReactNode } from 'react';

import { listUpstreamModels, type ProviderProfiles } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';

/** 填的是变量引用时，别处看不到它当前指向谁。改动过就不显示：展开值配不上刚敲进去的变量名。 */
export const EnvHint = ({
  raw, savedRaw, resolved,
}: { raw: string; savedRaw: string; resolved: string }) => {
  if (raw !== savedRaw || !raw || raw === resolved) return null;
  // 说的是哪个字段由位置交代：这一行就贴在那个输入框下面，且与它左对齐。
  return (
    <p className="qc-cap-desc qc-chan-note">
      {resolved ? '解析为 ' + resolved : '环境变量未设置，解析为空'}
    </p>
  );
};

const hostOf = (raw: string): string => {
  const value = (raw || '').trim();
  if (!value) return '';
  try {
    return new URL(value.includes('://') ? value : 'https://' + value).hostname.toLowerCase();
  } catch {
    // 变量引用和半截地址都会走到这儿，认不出就是认不出。
    return '';
  }
};

export interface HostMismatch {
  looksLike: string;
  wire: string;
}

/**
 * 这个地址是别家官方域名、而且当前渠道收不下时，返回这两家是谁。
 *
 * 只认官方域名。中转站的域名看不出出身，认不出一律放行 —— 拦错比放过更烦人。
 */
export const hostMismatch = (
  baseUrl: string, provider: string, profiles: ProviderProfiles | null,
): HostMismatch | null => {
  if (!profiles || !provider) return null;
  const host = hostOf(baseUrl);
  if (!host) return null;
  const accepted = profiles.hostProfiles[host];
  if (!accepted || accepted.length === 0) return null;
  const wire = profiles.wireFormats[provider] || provider;
  if (accepted.includes(wire)) return null;
  return { looksLike: accepted.join(' 或 '), wire };
};

/** 存照存，只是让人一眼看出来填串了。 */
export const HostMismatchHint = ({
  baseUrl, provider, profiles,
}: { baseUrl: string; provider: string; profiles: ProviderProfiles | null }) => {
  const mismatch = hostMismatch(baseUrl, provider, profiles);
  if (!mismatch) return null;
  return (
    <p className="qc-mismatch">
      这个地址是 {mismatch.looksLike} 的，而 {provider} 按 {mismatch.wire} 的格式发请求，会被对面拒掉。
      真要换家的话，新建一个 {mismatch.looksLike} 渠道再填。
    </p>
  );
};

/** 标签一列、内容一列。渠道页和系统槽位共用，改布局只动这一处。 */
export const FieldRow = ({ label, children }: { label: string; children: ReactNode }) => (
  <div className="qc-chan-row">
    <span className="qc-chan-label">{label}</span>
    <div className="qc-chan-body">{children}</div>
  </div>
);

/**
 * 模型名一行：手填 + 从上游拉真实列表。
 *
 * 拉取用的是页面上当前的地址和密钥，不是存盘的那份 —— 改完还没保存时也要能试。
 * 密钥留空表示沿用已存的，后端自己去取，明文不用往回传。
 */
export const ModelField = ({
  provider, value, baseUrl, apiKey, choices, listId, ariaLabel, slot, onChange,
}: {
  provider: string;
  value: string;
  baseUrl: string;
  apiKey: string;
  /** 别处已经在用的模型名。上游定义值空间，这只是提示，不是封闭集合。 */
  choices: string[];
  listId: string;
  ariaLabel: string;
  /** 在编辑某个系统槽位时给上。密钥框留空时，后端要拿这一槽存的那把去问。 */
  slot?: string;
  onChange: (next: string) => void;
}) => {
  const token = useSettingsStore((state) => state.adminToken);
  const [pulled, setPulled] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');

  // 拉到的排前面：那是这家真有的，比从别处抄来的名字可信。
  const options = useMemo(
    () => Array.from(new Set([...pulled, ...choices])).filter(Boolean),
    [pulled, choices],
  );

  const pull = async () => {
    if (!token) return;
    setBusy(true);
    setNote('');
    try {
      const models = await listUpstreamModels(token, provider, {
        base_url: baseUrl,
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        ...(slot ? { slot } : {}),
      });
      setPulled(models);
      setNote(models.length + ' 个模型，点输入框选');
    } catch (caught) {
      setNote(caught instanceof Error ? caught.message : String(caught));
    }
    setBusy(false);
  };

  return (
    <>
      <FieldRow label="模型">
        <input
          aria-label={ariaLabel}
          className="qc-inp"
          list={options.length ? listId : undefined}
          onChange={(event) => onChange(event.target.value)}
          placeholder="模型名"
          value={value}
        />
        {options.length > 0 && (
          <datalist id={listId}>
            {options.map((model) => <option key={model} value={model} />)}
          </datalist>
        )}
        <button
          className="qc-btn qc-btn-quiet"
          disabled={busy || !token}
          onClick={() => void pull()}
          title="用当前填的地址和密钥问上游要模型列表"
          type="button"
        >
          {busy ? '拉取中' : '拉取'}
        </button>
      </FieldRow>
      {note && <p className="qc-cap-desc qc-chan-note">{note}</p>}
    </>
  );
};
