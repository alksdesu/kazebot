// 模型与渠道的只读概览。增删改、切活跃、备选链都在 QQ 控制台的「渠道」「模型」两页里，
// 单一写入点 —— 两边都能改的话，同一份 config.yaml 没有乐观锁，后存的会盖掉先存的。
import { useEffect, useState } from 'react';

import {
  type ProviderConfigPublic,
  type ProvidersResponse,
  getProviders,
  reloadConfig,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';

// 值是变量引用时，光看原文不知道它当前指向谁。
const Value = ({ raw, resolved }: { raw: string; resolved: string }) => (
  <>
    <p className="mb-1 font-mono text-sm text-[var(--duties-text)]">{raw || '—'}</p>
    {raw && raw !== resolved && (
      <p className="mb-3 font-mono text-[0.65rem] text-[var(--duties-tertiary)]">
        {resolved ? `解析为 ${resolved}` : '环境变量未设置，解析为空'}
      </p>
    )}
  </>
);

const Line = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <>
    <p className="mb-1 text-xs text-[var(--duties-secondary)]">{label}</p>
    {children}
  </>
);

const ProviderCard = ({ name, cfg, active }: { name: string; cfg: ProviderConfigPublic; active: boolean }) => (
  <div
    className={`border bg-[var(--duties-panel)] p-4 ${
      active ? 'border-[var(--duties-text)]' : 'border-[var(--duties-border)]'
    }`}
  >
    <div className="mb-3 flex items-center gap-2">
      <p className="font-mono text-sm font-semibold text-[var(--duties-text)]">{name}</p>
      {active && (
        <span className="rounded bg-[var(--duties-text)] px-1.5 py-0.5 text-[0.6rem] font-bold text-[var(--duties-bg)]">
          ACTIVE
        </span>
      )}
    </div>

    <Line label="模型">
      <Value raw={cfg.model_raw} resolved={cfg.model} />
    </Line>
    <Line label="基础 URL">
      <Value raw={cfg.base_url_raw} resolved={cfg.base_url} />
    </Line>
    <Line label="API 密钥">
      <p className="font-mono text-sm text-[var(--duties-text)]">
        {cfg.api_key_present ? `已设置 ${cfg.api_key_redacted}` : '未设置'}
      </p>
    </Line>
  </div>
);

export const ModelSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();

  const [data, setData] = useState<ProvidersResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    setMsg('');
    try {
      setData(await getProviders(adminToken));
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [adminToken, isAuthenticated]);

  const handleReload = async () => {
    if (!adminToken) return;
    setMsg('');
    try {
      await reloadConfig(adminToken);
      await load();
      setMsg('配置已重载');
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '重载失败');
    }
  };

  return (
    <section className="mx-auto flex h-full max-w-4xl flex-col gap-5 overflow-y-auto p-4 sm:p-6">
      <div>
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">模型与渠道</h2>
        <p className="mt-2 max-w-2xl text-sm text-[var(--duties-secondary)]">
          这里只看不改。密钥、地址、模型和活跃渠道在 QQ 控制台的「渠道」页里改，请求参数和备选链在「模型」页。
        </p>
      </div>

      {!isAuthenticated ? (
        <div className="border border-[var(--duties-border)] p-4 text-sm text-[var(--duties-secondary)]">
          查看模型设置前需要完成管理员令牌认证。
        </div>
      ) : loading && !data ? (
        <div className="p-4 text-sm text-[var(--duties-secondary)]">加载中...</div>
      ) : (
        <>
          {msg && (
            <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-4 py-2 text-xs text-[var(--duties-tertiary)]">
              {msg}
            </div>
          )}

          {data && Object.entries(data.providers).map(([name, cfg]) => (
            <ProviderCard active={data.active_provider === name} cfg={cfg} key={name} name={name} />
          ))}

          {data && (
            <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
              <p className="mb-2 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">Fallback 链</p>
              {data.fallbacks.length === 0 ? (
                <p className="text-xs text-[var(--duties-secondary)]">没有备选渠道，主渠道失败时任务直接报错。</p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {data.fallbacks.map((fb, i) => (
                    <span
                      key={i}
                      className="border border-[var(--duties-border)] px-2 py-1 font-mono text-xs text-[var(--duties-secondary)]"
                    >
                      {i + 1}. {fb.provider || JSON.stringify(fb)}
                      {fb.model ? ` · ${fb.model}` : ''}
                      {fb.supports_vision ? ' · 可读图' : ''}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          <div className="flex items-center gap-3">
            <Button onClick={handleReload}>🔄 重载配置</Button>
            <p className="text-xs text-[var(--duties-secondary)]">重新读取 config.yaml，不中断服务</p>
          </div>
        </>
      )}
    </section>
  );
};
