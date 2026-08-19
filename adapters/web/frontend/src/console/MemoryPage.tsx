// 长期记忆与会话上下文。这一页直接对文件动手，不走顶部那条 qq.yaml 状态条。
import { useEffect, useMemo, useState } from 'react';

import {
  clearMemoryNamespace,
  deleteMemoryEntry,
  getConversationMessages,
  getConversations,
  getMemoryEntries,
  getMemoryOverview,
  resetConversationBySession,
  saveMemoryEntry,
  type ConversationRow,
  type MemoryEntry,
  type MemoryNamespace,
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

  const reset = async (row: ConversationRow) => {
    if (!adminToken) return;
    if (!window.confirm(`重置「${row.owner?.label || row.session_id}」的上下文？之后从头开始。`)) return;
    try {
      await resetConversationBySession(adminToken, row.session_id);
      setPreview(null);
      await load();
      setNote('已重置');
    } catch (error) { setNote(say(error)); }
  };

  return (
    <Block hint="重置会连带清掉 bot 侧的群消息缓存" title="会话上下文">
      {rows.length === 0 && <Empty>还没有任何会话。</Empty>}
      {rows.map((row) => (
        <div className="qc-mem-entry" key={row.session_id}>
          <div className="qc-mem-entry-head">
            <span className="qc-mem-name">{row.owner?.label || row.channel || '未知来源'}</span>
            <span className="qc-mem-count">{sizeText(row.bytes)}</span>
            <button className="qc-btn" onClick={() => void open(row)} type="button">
              {preview?.id === row.session_id ? '收起' : '查看'}
            </button>
            <button className="qc-btn" disabled={!row.conversation_key} onClick={() => void reset(row)} type="button">
              重置
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

export const MemoryPage = () => (
  <>
    <MemoryBlock />
    <ContextBlock />
  </>
);
