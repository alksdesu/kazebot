// 账号作用域搬迁。换号之后同一个群会各记各的，这里把旧号名下的会话与记忆整批划给新号。
import { useEffect, useRef, useState } from 'react';

import { getQqScope, migrateQqScope, type ScopeStatus } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Button, Footnote, Input } from './components';

const TEXT = 'mt-1.5 break-words text-xs leading-relaxed';
const KW = 'mt-1 break-words font-mono text-mini text-[var(--duties-secondary)]';
// 搬迁清单可能上百条，给个高度上限，不然整页被它撑开。
const LOG = 'mt-1.5 max-h-80 overflow-auto border-t border-[var(--duties-border)] pt-1.5';
const ADD = 'flex flex-col gap-1.5 border-t border-[var(--duties-border)] pt-2.5';
const FIELD = 'flex items-center gap-2';
const LABEL = 'w-14 flex-none text-xs text-[var(--duties-secondary)]';
const ID = 'font-mono text-xs';

const say = (error: unknown): string => (error instanceof Error ? error.message : '出错了');

export const AccountScope = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const [status, setStatus] = useState<ScopeStatus | null>(null);
  const [target, setTarget] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [previewTarget, setPreviewTarget] = useState('');
  const requestVersion = useRef(0);

  const load = async (withTarget = '') => {
    if (!adminToken) return;
    const version = ++requestVersion.current;
    setPreviewTarget('');
    setBusy(true);
    try {
      const result = await getQqScope(adminToken, withTarget);
      if (version !== requestVersion.current) return;
      setStatus(result);
      setPreviewTarget(withTarget);
    } catch (error) { if (version === requestVersion.current) setNote(say(error)); }
    finally { if (version === requestVersion.current) setBusy(false); }
  };

  useEffect(() => { void load(); return () => { requestVersion.current += 1; }; }, [adminToken]);

  const preview = async () => {
    if (busy || !target.trim()) return;
    setNote('');
    await load(target.trim());
  };

  const migrate = async () => {
    if (!adminToken || busy || !status?.preview || !previewTarget || previewTarget !== target.trim()) return;
    const to = target.trim();
    // 显示探测出的实际归属，而不是记录的当前账号 —— 存量数据两者往往不一样。
    const from = status?.preview?.source_scope || '无归属';
    if (!window.confirm(`把全部会话与长期记忆从「${from}」搬到「${to}」？搬之前会自动备份。`)) return;
    setBusy(true);
    try {
      const result = await migrateQqScope(adminToken, to);
      setNote(`已搬迁 ${result.moved_memory_dirs} 份记忆、${result.sessions_changed} 个会话。备份在 ${result.backup_dir}`);
      await load();
    } catch (error) { setNote(say(error)); } finally { setBusy(false); }
  };

  const plan = previewTarget && previewTarget === target.trim() ? status?.preview : undefined;
  const movable = plan?.conversations.filter((row) => !row.blocked) ?? [];

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

      {plan && (
        <div className={LOG}>
          <p className={KW}>
            {plan.conversations.length === 0
              ? '这批数据已经在这个号名下了，无需搬迁。'
              : `这批数据现属「${plan.source_scope || '无归属'}」，`
                + `${plan.conversations.length} 个会话待搬，其中 ${movable.length} 个可以直接搬。`}
          </p>
          {plan.conversations.map((row) => (
            <p className={TEXT} key={row.old_namespace}>
              <span className={ID}>{row.old_namespace.slice(0, 12)}…</span>
              {' → '}
              {row.new_namespace.slice(0, 12)}…
              {!row.has_memory && ' （没有长期记忆）'}
              {row.blocked && ' （目标已存在，跳过）'}
            </p>
          ))}
          {plan.unknown_namespaces.length > 0 && (
            <p className={KW}>
              另有 {plan.unknown_namespaces.length} 份记忆查不到归属，搬不了，会原样留着。
            </p>
          )}
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
