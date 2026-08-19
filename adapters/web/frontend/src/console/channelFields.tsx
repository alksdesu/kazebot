// 渠道编辑共用的小件。渠道页和系统槽位都摆 model/地址/密钥三项，回填规则也一样。
import type { ProviderProfiles } from '../api/supervisorClient';

/** 填的是变量引用时，别处看不到它当前指向谁。改动过就不显示：展开值配不上刚敲进去的变量名。 */
export const EnvHint = ({
  raw, savedRaw, resolved,
}: { raw: string; savedRaw: string; resolved: string }) => {
  if (raw !== savedRaw || !raw || raw === resolved) return null;
  return <p className="qc-cap-desc">{resolved ? '解析为 ' + resolved : '环境变量未设置，解析为空'}</p>;
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
