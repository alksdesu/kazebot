// 长期记忆与会话上下文。这一页直接对文件动手，不走顶部那条 qq.yaml 状态条。
import { useEffect, useMemo, useState } from 'react';

import {
  clearMemoryNamespace,
  deleteMemoryEntry,
  getConversationMessages,
  getConversations,
  getMemoryEntries,
  getMemoryOverview,
  getQqScope,
  migrateQqScope,
  resetConversationBySession,
  saveMemoryEntry,
  type ConversationRow,
  type MemoryEntry,
  type MemoryNamespace,
  type ScopeStatus,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Button, Check, Empty, Footnote, Input, Tag } from './components';
import { sizeText } from './format';

const EMPTY_DRAFT = { id: '', content: '', keywords: '', constant: false };

const ENTRY = 'mt-1.5 border border-[var(--duties-border)] bg-[var(--duties-panel)] px-2.5 py-2';
// 常驻条目每轮都进 prompt、一直占着注入预算，扫一眼就该看出是哪几条。
const ENTRY_CONST = 'border-l-[3px] border-l-[var(--duties-live)]';
const HEAD = 'flex flex-wrap items-center gap-1.5';
const ID = 'font-mono text-xs';
const META = 'font-mono text-[0.65rem] text-[var(--duties-tertiary)]';
const TEXT = 'mt-1.5 break-words text-xs leading-relaxed';
const KW = 'mt-1 break-words font-mono text-[0.65rem] text-[var(--duties-secondary)]';
// 上下文预览可能上百条，给个高度上限，不然整页被它撑开。
const LOG = 'mt-1.5 max-h-80 overflow-auto border-t border-[var(--duties-border)] pt-1.5';
const ADD = 'flex flex-col gap-1.5 border-t border-[var(--duties-border)] pt-2.5';
const FIELD = 'flex items-center gap-2';
const LABEL = 'w-14 flex-none text-xs text-[var(--duties-secondary)]';
const BUCKET = 'font-mono text-[0.65rem] text-[var(--duties-secondary)]';
// 会话按登录账号分组时的小标题。只在换过号之后才出现。
const GROUP = 'mb-1.5 mt-3.5 text-xs text-[var(--duties-secondary)]';

const say = (error: unknown): string => (error instanceof Error ? error.message : '出错了');

// 群里说的话归群、关于某个人的归人、认不出出处的归通用。
const BUCKETS = ['群组', '人物', '通用'] as const;

function bucketOf(row: MemoryNamespace): (typeof BUCKETS)[number] {
  if (row.kind === 'subject' || row.owner.kind === 'private') return '人物';
  if (row.owner.kind === 'group' || row.owner.kind === 'agent') return '群组';
  return '通用';
}

const MemoryBlock = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const [namespaces, setNamespaces] = useState<MemoryNamespace[]>([]);
  const [selected, setSelected] = useState('');
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  const loadOverview = async () => {
    if (!adminToken) return;
    try { setNamespaces(await getMemoryOverview(adminToken)); } catch (error) { setNote(say(error)); }
  };

  const loadEntries = async (namespace: string) => {
    if (!adminToken || !namespace) { setEntries([]); return; }
    try { setEntries(await getMemoryEntries(adminToken, namespace)); } catch (error) { setNote(say(error)); }
  };

  useEffect(() => { void loadOverview(); }, [adminToken]);
  useEffect(() => { void loadEntries(selected); }, [selected]);

  // 固定按「群组 / 人物 / 通用」排，空的那档不显示。
  const grouped = useMemo(
    () => BUCKETS
      .map((bucket) => [bucket, namespaces.filter((row) => bucketOf(row) === bucket)] as const)
      .filter(([, rows]) => rows.length > 0),
    [namespaces],
  );

  const current = namespaces.find((row) => row.key === selected);
  const constantCount = entries.filter((entry) => entry.constant).length;

  const submit = async () => {
    if (!adminToken || !selected) return;
    setBusy(true);
    setNote('');
    try {
      await saveMemoryEntry(adminToken, selected, {
        id: draft.id.trim(),
        content: draft.content.trim(),
        keywords: draft.keywords.split(',').map((word) => word.trim()).filter(Boolean),
        constant: draft.constant,
      });
      setDraft(EMPTY_DRAFT);
      await loadEntries(selected);
      await loadOverview();
      setNote('已添加');
    } catch (error) {
      setNote(say(error));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (entry: MemoryEntry) => {
    if (!adminToken || !selected || !window.confirm(`删除「${entry.id}」？`)) return;
    try {
      await deleteMemoryEntry(adminToken, selected, entry.book, entry.id);
      await loadEntries(selected);
      await loadOverview();
    } catch (error) { setNote(say(error)); }
  };

  const clearAll = async () => {
    if (!adminToken || !selected) return;
    if (!window.confirm('清空这一份的全部记忆？不可恢复。')) return;
    try {
      const removed = await clearMemoryNamespace(adminToken, selected);
      setSelected('');
      await loadOverview();
      setNote(`已清空 ${removed} 条`);
    } catch (error) { setNote(say(error)); }
  };

  return (
    <Block hint="模型聊天时自己攒下来的。手动加的不会被自动清理" title="长期记忆">
      {namespaces.length === 0 && <Empty>还没有任何记忆。</Empty>}
      <div className="grid grid-cols-1 items-start gap-3.5 md:grid-cols-[15rem_1fr]">
        <div className="flex flex-col gap-2.5">
          {grouped.map(([label, rows]) => (
            <div className="flex flex-col gap-1" key={label}>
              <p className={BUCKET}>{label}</p>
              {rows.map((row) => (
                <button
                  aria-current={selected === row.key ? 'true' : undefined}
                  className="flex w-full items-baseline gap-2 border border-[var(--duties-border)] bg-[var(--duties-panel)] px-2.5 py-1.5 text-left text-xs hover:bg-[var(--duties-muted)] aria-[current=true]:border-[var(--duties-text)] aria-[current=true]:bg-[var(--duties-muted)]"
                  key={row.key}
                  onClick={() => setSelected(row.key)}
                  type="button"
                >
                  <span className="min-w-0 flex-1 truncate">{row.owner.label}</span>
                  <span className={META}>{row.entry_count}</span>
                </button>
              ))}
            </div>
          ))}
        </div>

        <div className="flex min-w-0 flex-col gap-2">
          {!selected && <Empty>左边选一项来查看。</Empty>}
          {selected && (
            <>
              <div className="flex items-center justify-between gap-2 font-mono text-xs">
                <span>
                  {current?.owner.label} · {entries.length} 条
                  {constantCount > 0 && (
                    <Tag className="ml-2" tone="live">其中 {constantCount} 条常驻</Tag>
                  )}
                </span>
                <Button onClick={clearAll}>清空</Button>
              </div>
              {entries.map((entry) => (
                <div
                  className={`${ENTRY}${entry.constant ? ` ${ENTRY_CONST}` : ''}`}
                  key={`${entry.book}/${entry.id}`}
                >
                  <div className={HEAD}>
                    <span className={ID}>{entry.id}</span>
                    {entry.constant && <Tag tone="live">常驻 · 每轮都注入</Tag>}
                    {entry.source === 'manual' && <Tag>手动</Tag>}
                    <span className="flex-1" />
                    <Button onClick={() => void remove(entry)}>删除</Button>
                  </div>
                  <p className={TEXT}>{entry.content}</p>
                  {!!entry.keywords?.length && (
                    <p className={KW}>关键词：{entry.keywords.join('、')}</p>
                  )}
                </div>
              ))}

              <div className={ADD}>
                <p className={BUCKET}>添加一条</p>
                <label className={FIELD}>
                  <span className={LABEL}>标识</span>
                  <Input
                    onChange={(event) => setDraft({ ...draft, id: event.target.value })}
                    placeholder="英文、数字、下划线"
                    value={draft.id}
                    width="flex"
                  />
                </label>
                <label className={FIELD}>
                  <span className={LABEL}>内容</span>
                  <Input
                    onChange={(event) => setDraft({ ...draft, content: event.target.value })}
                    value={draft.content}
                    width="flex"
                  />
                </label>
                <label className={FIELD}>
                  <span className={LABEL}>关键词</span>
                  <Input
                    onChange={(event) => setDraft({ ...draft, keywords: event.target.value })}
                    placeholder="逗号分隔，说到这些词时才会想起来"
                    value={draft.keywords}
                    width="flex"
                  />
                </label>
                <Check
                  align="start"
                  checked={draft.constant}
                  onChange={(checked) => setDraft({ ...draft, constant: checked })}
                  tone="muted"
                >
                  常驻：每轮都注入，不用关键词命中。占注入预算，只给必须一直记住的事。
                </Check>
                <Button
                  className="self-start"
                  disabled={busy || !draft.id.trim() || !draft.content.trim()}
                  onClick={submit}
                >
                  {busy ? '保存中' : '添加'}
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
      {note && <Footnote>{note}</Footnote>}
    </Block>
  );
};

const ContextBlock = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const [rows, setRows] = useState<ConversationRow[]>([]);
  const [preview, setPreview] = useState<{ id: string; total: number; messages: Array<Record<string, any>> } | null>(null);
  const [note, setNote] = useState('');

  const load = async () => {
    if (!adminToken) return;
    try { setRows(await getConversations(adminToken)); } catch (error) { setNote(say(error)); }
  };

  useEffect(() => { void load(); }, [adminToken]);

  const open = async (row: ConversationRow) => {
    if (!adminToken) return;
    if (preview?.id === row.session_id) { setPreview(null); return; }
    try {
      setPreview({ id: row.session_id, ...(await getConversationMessages(adminToken, row.session_id, 100)) });
    } catch (error) { setNote(say(error)); }
  };

  const drop = async (row: ConversationRow) => {
    if (!adminToken) return;
    const name = row.owner?.label || row.session_id;
    if (!window.confirm(`删除「${name}」的上下文？聊天记录清空，之后重新积累。长期记忆不受影响。`)) return;
    try {
      await resetConversationBySession(adminToken, row.session_id);
      setPreview(null);
      await load();
      setNote('已删除，下一条消息开始重新积累');
    } catch (error) { setNote(say(error)); }
  };

  const entry = (row: ConversationRow) => (
    <div className={ENTRY} key={row.session_id}>
      <div className={HEAD}>
        <span className="min-w-0 flex-1 truncate text-xs">{row.owner?.label || row.channel || '未知来源'}</span>
        <span className={META}>{sizeText(row.bytes)}</span>
        <Button onClick={() => void open(row)}>
          {preview?.id === row.session_id ? '收起' : '查看'}
        </Button>
        <Button
          disabled={!row.conversation_key}
          onClick={() => void drop(row)}
          title={row.conversation_key ? '' : '这条会话已经没有归属，只能在服务器上删'}
        >
          删除
        </Button>
      </div>
      {preview?.id === row.session_id && (
        <div className={LOG}>
          <p className={KW}>共 {preview.total} 条，显示最近 {preview.messages.length} 条</p>
          {preview.messages.map((msg, index) => (
            <p className={TEXT} key={index}>
              <span className={ID}>{String(msg.role || '')}</span>
              {' '}
              {String(msg.content || '').slice(0, 400)}
            </p>
          ))}
        </div>
      )}
    </div>
  );

  // 会话键摘要带 QQ 号，换号后同一个群另起一条。没换过号时 stale 为空，视图跟以前一样。
  const stale = rows.filter((row) => row.current_account === false);
  const mine = rows.filter((row) => row.current_account !== false);

  return (
    <Block hint="删除会连带清掉 bot 侧的消息缓存与附件；长期记忆另算" title="会话上下文">
      {rows.length === 0 && <Empty>还没有任何会话。</Empty>}
      {stale.length === 0 ? rows.map(entry) : (
        <>
          <p className={GROUP}>当前账号</p>
          {mine.length === 0 ? <Empty>这个号还没有任何会话。</Empty> : mine.map(entry)}
          <p className={GROUP}>其它账号 · 换号前留下的，上下文和长期记忆都不互通</p>
          {stale.map(entry)}
        </>
      )}
      {note && <Footnote>{note}</Footnote>}
    </Block>
  );
};

const ScopeBlock = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const [status, setStatus] = useState<ScopeStatus | null>(null);
  const [target, setTarget] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  const load = async (withTarget = '') => {
    if (!adminToken) return;
    try { setStatus(await getQqScope(adminToken, withTarget)); } catch (error) { setNote(say(error)); }
  };

  useEffect(() => { void load(); }, [adminToken]);

  const preview = async () => {
    setNote('');
    await load(target.trim());
  };

  const migrate = async () => {
    if (!adminToken) return;
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

  const plan = status?.preview;
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
            onChange={(event) => setTarget(event.target.value)}
            placeholder="目标 bot 的 QQ 号"
            value={target}
            width="flex"
          />
        </label>
        <Button className="self-start" disabled={!target.trim()} onClick={() => void preview()}>
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
              {busy ? '搬迁中…' : '执行搬迁'}
            </Button>
          )}
        </div>
      )}
      {note && <Footnote>{note}</Footnote>}
    </Block>
  );
};

export const MemoryPage = () => (
  <>
    <MemoryBlock />
    <ContextBlock />
    <ScopeBlock />
  </>
);
