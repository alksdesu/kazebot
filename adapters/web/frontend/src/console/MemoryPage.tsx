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
import { Block, Empty, Footnote } from './components';

const EMPTY_DRAFT = { id: '', content: '', keywords: '', constant: false };

const say = (error: unknown): string => (error instanceof Error ? error.message : '出错了');

// 群里说的话归群、关于某个人的归人、认不出出处的归通用。
const BUCKETS = ['群组', '人物', '通用'] as const;

function bucketOf(row: MemoryNamespace): (typeof BUCKETS)[number] {
  if (row.kind === 'subject' || row.owner.kind === 'private') return '人物';
  if (row.owner.kind === 'group' || row.owner.kind === 'agent') return '群组';
  return '通用';
}

function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
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
      <div className="qc-mem">
        <div className="qc-mem-list">
          {grouped.map(([label, rows]) => (
            <div key={label}>
              <p className="qc-mem-bucket">{label}</p>
              {rows.map((row) => (
                <button
                  aria-current={selected === row.key ? 'true' : undefined}
                  className="qc-mem-item"
                  key={row.key}
                  onClick={() => setSelected(row.key)}
                  type="button"
                >
                  <span className="qc-mem-name">{row.owner.label}</span>
                  <span className="qc-mem-count">{row.entry_count}</span>
                </button>
              ))}
            </div>
          ))}
        </div>

        <div className="qc-mem-body">
          {!selected && <Empty>左边选一项来查看。</Empty>}
          {selected && (
            <>
              <div className="qc-mem-head">
                <span>
                  {current?.owner.label} · {entries.length} 条
                  {constantCount > 0 && (
                    <span className="qc-mem-const-note">其中 {constantCount} 条常驻</span>
                  )}
                </span>
                <button className="qc-btn" onClick={clearAll} type="button">清空</button>
              </div>
              {entries.map((entry) => (
                <div
                  className={`qc-mem-entry${entry.constant ? ' qc-mem-entry-const' : ''}`}
                  key={`${entry.book}/${entry.id}`}
                >
                  <div className="qc-mem-entry-head">
                    <span className="qc-mem-id">{entry.id}</span>
                    {entry.constant && <span className="qc-mem-tag qc-mem-tag-const">常驻 · 每轮都注入</span>}
                    {entry.source === 'manual' && <span className="qc-mem-tag">手动</span>}
                    <button className="qc-btn" onClick={() => void remove(entry)} type="button">删除</button>
                  </div>
                  <p className="qc-mem-text">{entry.content}</p>
                  {!!entry.keywords?.length && (
                    <p className="qc-mem-kw">关键词：{entry.keywords.join('、')}</p>
                  )}
                </div>
              ))}

              <div className="qc-mem-add">
                <p className="qc-mem-bucket">添加一条</p>
                <label className="qc-mem-field">
                  <span>标识</span>
                  <input
                    onChange={(event) => setDraft({ ...draft, id: event.target.value })}
                    placeholder="英文、数字、下划线"
                    value={draft.id}
                  />
                </label>
                <label className="qc-mem-field">
                  <span>内容</span>
                  <input
                    onChange={(event) => setDraft({ ...draft, content: event.target.value })}
                    value={draft.content}
                  />
                </label>
                <label className="qc-mem-field">
                  <span>关键词</span>
                  <input
                    onChange={(event) => setDraft({ ...draft, keywords: event.target.value })}
                    placeholder="逗号分隔，说到这些词时才会想起来"
                    value={draft.keywords}
                  />
                </label>
                <label className="qc-mem-check">
                  <input
                    checked={draft.constant}
                    onChange={(event) => setDraft({ ...draft, constant: event.target.checked })}
                    type="checkbox"
                  />
                  <span>常驻：每轮都注入，不用关键词命中。占注入预算，只给必须一直记住的事。</span>
                </label>
                <button
                  className="qc-btn qc-btn-primary"
                  disabled={busy || !draft.id.trim() || !draft.content.trim()}
                  onClick={submit}
                  type="button"
                >
                  {busy ? '保存中' : '添加'}
                </button>
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

  return (
    <Block hint="删除会连带清掉 bot 侧的消息缓存与附件；长期记忆另算" title="会话上下文">
      {rows.length === 0 && <Empty>还没有任何会话。</Empty>}
      {rows.map((row) => (
        <div className="qc-mem-entry" key={row.session_id}>
          <div className="qc-mem-entry-head">
            <span className="qc-mem-name">{row.owner?.label || row.channel || '未知来源'}</span>
            <span className="qc-mem-count">{sizeText(row.bytes)}</span>
            <button className="qc-btn" onClick={() => void open(row)} type="button">
              {preview?.id === row.session_id ? '收起' : '查看'}
            </button>
            <button
              className="qc-btn"
              disabled={!row.conversation_key}
              onClick={() => void drop(row)}
              title={row.conversation_key ? '' : '这条会话已经没有归属，只能在服务器上删'}
              type="button"
            >
              删除
            </button>
          </div>
          {preview?.id === row.session_id && (
            <div className="qc-mem-log">
              <p className="qc-mem-kw">共 {preview.total} 条，显示最近 {preview.messages.length} 条</p>
              {preview.messages.map((msg, index) => (
                <p className="qc-mem-text" key={index}>
                  <span className="qc-mem-id">{String(msg.role || '')}</span>
                  {' '}
                  {String(msg.content || '').slice(0, 400)}
                </p>
              ))}
            </div>
          )}
        </div>
      ))}
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
    const from = status?.current_scope || '无归属';
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
      <p className="qc-mem-text">
        当前归属：<strong>{status?.current_scope || '还没绑定账号'}</strong>
        {status?.bot_alive && status?.live_scope && status.live_scope !== status.current_scope
          && `（bot 正跑在 ${status.live_scope}）`}
      </p>
      <div className="qc-mem-add">
        <label className="qc-mem-field">
          <span>搬到</span>
          <input
            inputMode="numeric"
            onChange={(event) => setTarget(event.target.value)}
            placeholder="目标 bot 的 QQ 号"
            value={target}
          />
        </label>
        <button className="qc-btn" disabled={!target.trim()} onClick={() => void preview()} type="button">
          预览
        </button>
      </div>

      {plan && (
        <div className="qc-mem-log">
          <p className="qc-mem-kw">
            {plan.conversations.length === 0
              ? '这批数据已经在这个号名下了，无需搬迁。'
              : `${plan.conversations.length} 个会话待搬，其中 ${movable.length} 个可以直接搬。`}
          </p>
          {plan.conversations.map((row) => (
            <p className="qc-mem-text" key={row.old_namespace}>
              <span className="qc-mem-id">{row.old_namespace.slice(0, 12)}…</span>
              {' → '}
              {row.new_namespace.slice(0, 12)}…
              {!row.has_memory && ' （没有长期记忆）'}
              {row.blocked && ' （目标已存在，跳过）'}
            </p>
          ))}
          {plan.unknown_namespaces.length > 0 && (
            <p className="qc-mem-kw">
              另有 {plan.unknown_namespaces.length} 份记忆查不到归属，搬不了，会原样留着。
            </p>
          )}
          {status?.bot_alive ? (
            <Footnote>bot 还在跑。它内存里存着按旧账号算的会话键，先停掉 bot 再搬。</Footnote>
          ) : (
            <button
              className="qc-btn"
              disabled={busy || movable.length === 0}
              onClick={() => void migrate()}
              type="button"
            >
              {busy ? '搬迁中…' : '执行搬迁'}
            </button>
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
