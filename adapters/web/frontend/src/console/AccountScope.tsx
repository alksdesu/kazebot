// 账号作用域搬迁。换号之后同一个群会各记各的，这里把旧号名下的会话与记忆整批划给新号。
import { useEffect, useRef, useState } from 'react';

import { getAccountScope, migrateAccountScope, type ScopeStatus, type ScopeStoreCount } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Button, Footnote, Input } from './components';
import { usePlatform } from './platformContext';

const TEXT = 'mt-1.5 break-words text-xs leading-relaxed';
const KW = 'mt-1 break-words font-mono text-mini text-[var(--duties-secondary)]';
// 搬迁清单可能上百条，给个高度上限，不然整页被它撑开。
const LOG = 'mt-1.5 max-h-80 overflow-auto border-t border-[var(--duties-border)] pt-1.5';
const ADD = 'flex flex-col gap-1.5 border-t border-[var(--duties-border)] pt-2.5';
const FIELD = 'flex items-center gap-2';
const LABEL = 'w-14 flex-none text-xs text-[var(--duties-secondary)]';
const ID = 'font-mono text-xs';

const say = (error: unknown): string => (error instanceof Error ? error.message : '出错了');
const scopeName = (scope: string): string => scope || '无归属';
// 和后端 format_store_counts 同一个说法。
const storeSummary = (stores: Record<string, ScopeStoreCount>): string => Object.values(stores)
  .filter((count) => count.moved || count.replaced)
  .map((count) => `${count.label} ${count.moved} 行${count.replaced ? `（顶掉目标 ${count.replaced} 行）` : ''}`)
  .join('；') || '无';

export const AccountScope = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const platformId = usePlatform().id;
  const [status, setStatus] = useState<ScopeStatus | null>(null);
  const [target, setTarget] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [previewTarget, setPreviewTarget] = useState('');
  const requestVersion = useRef(0);

  const load = async (withTarget = '', withSource?: string) => {
    if (!adminToken) return;
    const version = ++requestVersion.current;
    setPreviewTarget('');
    setBusy(true);
    try {
      const result = await getAccountScope(adminToken, platformId, withTarget, withSource);
      if (version !== requestVersion.current) return;
      setStatus(result);
      setPreviewTarget(withTarget);
    } catch (error) { if (version === requestVersion.current) setNote(say(error)); }
    finally { if (version === requestVersion.current) setBusy(false); }
  };

  useEffect(() => { void load(); return () => { requestVersion.current += 1; }; }, [adminToken, platformId]);

  const preview = async (source?: string) => {
    if (busy || !target.trim()) return;
    setNote('');
    await load(target.trim(), source);
  };

  const migrate = async () => {
    if (!adminToken || busy || !status?.preview || !previewTarget || previewTarget !== target.trim()) return;
    const to = target.trim();
    // 显示探测出的实际归属，而不是记录的当前账号 —— 存量数据两者往往不一样。
    const from = status.preview.source_scope;
    if (!window.confirm(`把「${scopeName(from)}」名下的会话与长期记忆搬到「${to}」？搬之前会自动备份。`)) return;
    setBusy(true);
    try {
      // 按预览认定的源账号搬：预览之后盘上多出别的账号的会话，也不会换成搬另一批。
      const result = await migrateAccountScope(adminToken, platformId, to, from);
      setNote(`已搬迁 ${result.moved_memory_dirs} 份记忆、${result.sessions_changed} 个会话。备份在 ${result.backup_dir}`);
      await load();
    } catch (error) { setNote(say(error)); } finally { setBusy(false); }
  };

  const current = previewTarget && previewTarget === target.trim();
  const plan = current ? status?.preview : undefined;
  const choices = current ? status?.ambiguous_sources ?? [] : [];
  const movable = plan?.conversations.filter((row) => !row.blocked) ?? [];
  const stranded = plan ? plan.unknown_namespaces.length + plan.unresolved_namespaces.length : 0;

  return (
    <Block hint="换号后同一个群会各记各的；搬迁把旧号名下的全部数据划给新号" title="记忆归属">
      <p className={TEXT}>
        当前归属：<strong>{status?.current_scope || '还没绑定账号'}</strong>
        {status?.bot_alive && status?.live_scope && status.live_scope !== status.current_scope
          && `（bot 正跑在 ${status.live_scope}）`}
      </p>
      <div className={ADD}>
        <label className={FIELD}>
          <span className={LABEL}>搬到</span>
          <Input
            inputMode="numeric"
            disabled={busy}
            onChange={(event) => { setTarget(event.target.value); setPreviewTarget(''); }}
            placeholder="目标 bot 的 QQ 号"
            value={target}
            width="flex"
          />
        </label>
        <Button className="self-start" disabled={busy || !target.trim()} onClick={() => void preview()} tone="quiet">
          预览
        </Button>
      </div>

      {choices.length > 0 && (
        <div className={ADD}>
          <p className={KW}>盘上的会话分属几个账号，一次搬一批，先选搬哪一个：</p>
          {choices.map((choice) => (
            <Button
              className="self-start"
              disabled={busy}
              key={choice.scope}
              onClick={() => void preview(choice.scope)}
              tone="quiet"
            >
              「{scopeName(choice.scope)}」名下的 {choice.conversations} 个会话
            </Button>
          ))}
        </div>
      )}

      {plan && (
        <div className={LOG}>
          <p className={KW}>
            {plan.conversations.length === 0
              ? '这批数据已经在这个号名下了，无需搬迁。'
              : `这批数据现属「${scopeName(plan.source_scope)}」，`
                + `${plan.conversations.length} 个会话待搬，其中 ${movable.length} 个可以直接搬。`}
          </p>
          {movable.length > 0 && <p className={KW}>功能数据跟着改写：{storeSummary(plan.stores)}</p>}
          {plan.conversations.map((row) => (
            <p className={TEXT} key={row.old_namespace}>
              <span className={ID}>{row.old_namespace.slice(0, 12)}…</span>
              {' → '}
              {row.new_namespace.slice(0, 12)}…
              {!row.has_memory && ' （没有长期记忆）'}
              {row.blocked && ' （两边都有记忆，跳过）'}
            </p>
          ))}
          {stranded > 0 && (
            <p className={KW}>
              另有 {stranded} 份记忆查不到归属，搬不了，会原样留着。
            </p>
          )}
          {plan.other_scopes.map((other) => (
            <p className={KW} key={other.scope}>
              「{scopeName(other.scope)}」名下还有 {other.conversations} 个会话，不在这次搬迁里。
            </p>
          ))}
          {status?.bot_alive ? (
            <Footnote>bot 还在跑。它内存里存着按旧账号算的会话键，先停掉 bot 再搬。</Footnote>
          ) : (
            <Button disabled={busy || movable.length === 0} onClick={() => void migrate()}>
              {busy ? '正在搬迁…' : '执行搬迁'}
            </Button>
          )}
        </div>
      )}
      {note && <Footnote>{note}</Footnote>}
    </Block>
  );
};
