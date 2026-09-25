// 长期记忆与会话上下文。这一页直接对文件动手，不走顶部那条 qq.yaml 状态条。
import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';

import {
  clearMemoryNamespace,
  deleteMemoryEntry,
  getConversationMessages,
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
import { Block, Button, Check, Empty, Footnote, Input, Pager, Tag } from './components';
import { sizeText } from './format';
import { useConversationDirectory } from '../features/useConversationDirectory';
import { useConversationNames } from '../features/useConversationNames';
import { scopeIdentity } from '../features/conversationNames';
import { useRequestScope } from '../features/asyncState';

const EMPTY_DRAFT = { id: '', content: '', keywords: '', constant: false };

// 三处列表都会随使用量无限变长，右边内容却还是那么点 —— 分页管住高度。
// 数值取到各自那一栏跟邻栏高度相当为止。
const SOURCE_PAGE = 10;
const ENTRY_PAGE = 12;
const CONV_PAGE = 15;

// 末页被删空之后要退回上一页，否则界面停在一片空白上，看着像数据没了。
const lastOffset = (count: number, size: number) =>
  Math.max(0, Math.floor((count - 1) / size) * size);

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
  const [loading, setLoading] = useState(false);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [entriesScope, setEntriesScope] = useState('');
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const entryVersion = useRef(0);
  const confirmDiscard = useUnsavedChanges(JSON.stringify(draft) !== JSON.stringify(EMPTY_DRAFT), '记忆草稿尚未保存，确定切换来源或离开吗？');
  const [query, setQuery] = useState('');
  const [sourceOffset, setSourceOffset] = useState(0);
  const [entryOffset, setEntryOffset] = useState(0);
  const namespaceNames = useConversationNames(namespaces.map(row => ({ scope: row.owner.conversation_key || row.namespace || row.key, owner: row.owner })));
  const describedNamespaces = useMemo(() => namespaces.map(row => ({ ...row, owner: { ...row.owner, label: namespaceNames.get(scopeIdentity(row.owner.conversation_key || row.namespace || row.key))! } })), [namespaces, namespaceNames]);

  const loadOverview = async () => {
    if (!adminToken) return;
    setOverviewLoading(true);
    try { setNamespaces(await getMemoryOverview(adminToken)); } catch (error) { setNote(say(error)); } finally { setOverviewLoading(false); }
  };

  const loadEntries = async (namespace: string) => {
    const version = ++entryVersion.current;
    if (!adminToken || !namespace) { setEntries([]); return; }
    setLoading(true);
    try {
      const result = await getMemoryEntries(adminToken, namespace);
      if (version !== entryVersion.current || selectedRef.current !== namespace) return;
      setEntries(result); setEntriesScope(namespace);
    } catch (error) {
      if (version === entryVersion.current && selectedRef.current === namespace) setNote(say(error));
    } finally {
      if (version === entryVersion.current) setLoading(false);
    }
  };

  useEffect(() => { void loadOverview(); }, [adminToken]);
  useEffect(() => { setEntries([]); setEntriesScope(''); void loadEntries(selected); setEntryOffset(0); return () => { ++entryVersion.current; }; }, [selected, adminToken]);

  // 固定按「群组 / 人物 / 通用」排，拍平成一条序列再分页 —— 按组各自分页会在一栏
  // 里挂出三个分页器。组标题在每页按需重新出现，翻页不会丢掉这一项属于哪一档。
  const sources = useMemo(() => {
    const wanted = query.trim().toLowerCase();
    return BUCKETS
      .flatMap((bucket) => describedNamespaces
        .filter((row) => bucketOf(row) === bucket)
        .map((row) => ({ bucket, row })))
      .filter(({ row }) => !wanted || row.owner.label.toLowerCase().includes(wanted));
  }, [describedNamespaces, query]);

  useEffect(() => {
    if (sourceOffset > 0 && sourceOffset >= sources.length) {
      setSourceOffset(lastOffset(sources.length, SOURCE_PAGE));
    }
  }, [sources.length, sourceOffset]);

  useEffect(() => {
    if (entryOffset > 0 && entryOffset >= entries.length) {
      setEntryOffset(lastOffset(entries.length, ENTRY_PAGE));
    }
  }, [entries.length, entryOffset]);

  const pageSources = sources.slice(sourceOffset, sourceOffset + SOURCE_PAGE);
  const pageEntries = entriesScope === selected ? entries.slice(entryOffset, entryOffset + ENTRY_PAGE) : [];
  const current = describedNamespaces.find((row) => row.key === selected);
  const constantCount = entries.filter((entry) => entry.constant).length;

  const submit = async () => {
    if (!adminToken || !selected || busy || loading || entriesScope !== selected) return;
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
    if (!adminToken || !selected || busy || loading || entriesScope !== selected || !window.confirm(`从「${current?.owner.label || '所选来源'}」删除「${entry.id}」？`)) return;
    setBusy(true);
    try {
      await deleteMemoryEntry(adminToken, selected, entry.book, entry.id);
      await loadEntries(selected);
      await loadOverview();
    } catch (error) { setNote(say(error)); } finally { setBusy(false); }
  };

  const clearAll = async () => {
    if (!adminToken || !selected || busy || loading || entriesScope !== selected) return;
    if (!window.confirm(`清空「${current?.owner.label || '所选来源'}」的全部记忆？不可恢复。`)) return;
    setBusy(true);
    try {
      const removed = await clearMemoryNamespace(adminToken, selected);
      setSelected('');
      setDraft(EMPTY_DRAFT);
      await loadOverview();
      setNote(`已清空 ${removed} 条`);
    } catch (error) { setNote(say(error)); } finally { setBusy(false); }
  };

  return (
    <Block hint="模型聊天时自己攒下来的。手动加的不会被自动清理" title="长期记忆">
      {overviewLoading ? <Empty>正在读取记忆来源…</Empty> : namespaces.length === 0 && <Empty>{note || '还没有任何记忆。'}</Empty>}
      <div className="grid grid-cols-1 items-start gap-3.5 md:grid-cols-[15rem_1fr]">
        <div className="flex flex-col gap-1">
          <Input
            aria-label="按名字筛选来源"
            onChange={(event) => { setQuery(event.target.value); setSourceOffset(0); }}
            placeholder="筛选来源"
            value={query}
            width="flex"
          />
          {sources.length === 0 && namespaces.length > 0 && <Empty>没有名字含「{query}」的来源。</Empty>}
          {pageSources.map(({ bucket, row }, index) => (
            <Fragment key={row.key}>
              {(index === 0 || pageSources[index - 1].bucket !== bucket) && (
                // 组间比组内多空一档；容器的 gap 只有一个值，差额补在这儿。
                <p className={`${BUCKET}${index === 0 ? ' mt-1.5' : ' mt-2.5'}`}>{bucket}</p>
              )}
              <button
                disabled={busy}
                aria-current={selected === row.key ? 'true' : undefined}
                className="flex w-full items-baseline gap-2 border border-[var(--duties-border)] bg-[var(--duties-panel)] px-2.5 py-1.5 text-left text-xs hover:bg-[var(--duties-muted)] aria-[current=true]:border-[var(--duties-text)] aria-[current=true]:bg-[var(--duties-muted)]"
                onClick={() => { if (row.key !== selected && confirmDiscard()) { setEntries([]); setEntriesScope(''); setDraft(EMPTY_DRAFT); setNote(''); setSelected(row.key); } }}
                type="button"
              >
                <span className="min-w-0 flex-1 truncate">{row.owner.label}</span>
                <span className={META}>{row.entry_count}</span>
              </button>
            </Fragment>
          ))}
          <Pager
            offset={sourceOffset}
            onOffset={setSourceOffset}
            pageSize={SOURCE_PAGE}
            total={sources.length}
            unit="项"
          />
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
                <Button disabled={busy || loading || entriesScope !== selected} onClick={clearAll}>清空</Button>
              </div>
              {loading && <Empty>正在读取该来源的记忆…</Empty>}
              {pageEntries.map((entry) => (
                <div
                  className={`${ENTRY}${entry.constant ? ` ${ENTRY_CONST}` : ''}`}
                  key={`${entry.book}/${entry.id}`}
                >
                  <div className={HEAD}>
                    <span className={ID}>{entry.id}</span>
                    {entry.constant && <Tag tone="live">常驻 · 每轮都注入</Tag>}
                    {entry.source === 'manual' && <Tag>手动</Tag>}
                    <span className="flex-1" />
                    <Button disabled={busy || loading} onClick={() => void remove(entry)}>删除</Button>
                  </div>
                  <p className={TEXT}>{entry.content}</p>
                  {!!entry.keywords?.length && (
                    <p className={KW}>关键词：{entry.keywords.join('、')}</p>
                  )}
                </div>
              ))}
              <Pager
                offset={entryOffset}
                onOffset={setEntryOffset}
                pageSize={ENTRY_PAGE}
                total={entries.length}
                unit="条"
              />

              <fieldset disabled={busy || loading || entriesScope !== selected} className={ADD}>
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
              </fieldset>
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
  const { rows, error: directoryError, loading: directoryLoading, reload: load } = useConversationDirectory();
  const identity = useRequestScope(adminToken || '');
  const names = useConversationNames(rows.map(row => ({ scope: row.conversation_key || `session:${row.session_id}`, owner: row.owner, current_account: row.current_account })));
  const labelFor = (row: ConversationRow) => names.get(scopeIdentity(row.conversation_key || `session:${row.session_id}`)) || '会话（名称暂不可用）';
  const [preview, setPreview] = useState<{ id: string; total: number; messages: Array<Record<string, any>> } | null>(null);
  const [note, setNote] = useState('');
  const [offset, setOffset] = useState(0);
  const previewVersion = useRef(0);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => () => { previewVersion.current += 1; }, [adminToken]);

  const open = async (row: ConversationRow) => {
    if (!adminToken || busy) return;
    const version = ++previewVersion.current;
    if (preview?.id === row.session_id) { setPreview(null); setLoading(false); return; }
    setPreview(null);
    setLoading(true);
    setNote('');
    try {
      const result = await getConversationMessages(adminToken, row.session_id, 100);
      if (identity.isCurrent() && version === previewVersion.current) setPreview({ id: row.session_id, ...result });
    } catch (error) { if (identity.isCurrent() && version === previewVersion.current) setNote(say(error)); }
    finally { if (identity.isCurrent() && version === previewVersion.current) setLoading(false); }
  };

  const drop = async (row: ConversationRow) => {
    if (!adminToken || busy) return;
    const name = labelFor(row);
    if (!window.confirm(`删除「${name}」的上下文？聊天记录清空，之后重新积累。长期记忆不受影响。`)) return;
    setBusy(true);
    previewVersion.current += 1;
    setLoading(false);
    try {
      await resetConversationBySession(adminToken, row.session_id);
      if (!identity.isCurrent()) return;
      setPreview(null);
      await load();
      if (!identity.isCurrent()) return;
      setNote('已删除，下一条消息开始重新积累');
    } catch (error) { if (identity.isCurrent()) setNote(say(error)); } finally { if (identity.isCurrent()) setBusy(false); }
  };

  const entry = (row: ConversationRow) => (
    <div className={ENTRY} key={row.session_id}>
      <div className={HEAD}>
        <span className="min-w-0 flex-1 truncate text-xs">{labelFor(row)}</span>
        <span className={META}>{sizeText(row.bytes)}</span>
        <Button disabled={busy} onClick={() => void open(row)}>
          {preview?.id === row.session_id ? '收起' : '查看'}
        </Button>
        <Button
          disabled={busy || !row.conversation_key}
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

  // 拍平后再分页，两段各自分页会挂出两个分页器。没换过号时只有一段，标题不出现。
  const listed = stale.length === 0
    ? rows.map((row) => ({ group: '', row }))
    : [
      ...mine.map((row) => ({ group: '当前账号', row })),
      ...stale.map((row) => ({ group: '其它账号 · 换号前留下的，上下文和长期记忆都不互通', row })),
    ];

  useEffect(() => {
    if (offset > 0 && offset >= listed.length) setOffset(lastOffset(listed.length, CONV_PAGE));
  }, [listed.length, offset]);

  const page = listed.slice(offset, offset + CONV_PAGE);

  return (
    <Block hint="删除会连带清掉 bot 侧的消息缓存与附件；长期记忆另算" title="会话上下文">
      {loading && <Footnote>正在读取会话…</Footnote>}
      {directoryLoading && <Footnote>正在读取会话名称…</Footnote>}
      {directoryError && <Footnote>{directoryError} <Button onClick={() => void load()}>重试会话列表</Button></Footnote>}
      {!directoryLoading && !directoryError && rows.length === 0 && <Empty>还没有任何会话。</Empty>}
      {/* 空的那一段拍平后不留痕迹，可「这个号一条都没有」正是要说的话，所以单拎出来。 */}
      {stale.length > 0 && mine.length === 0 && (
        <>
          <p className={GROUP}>当前账号</p>
          <Empty>这个号还没有任何会话。</Empty>
        </>
      )}
      {page.map(({ group, row }, index) => (
        <Fragment key={row.session_id}>
          {group && (index === 0 || page[index - 1].group !== group) && (
            <p className={GROUP}>{group}</p>
          )}
          {entry(row)}
        </Fragment>
      ))}
      <Pager offset={offset} onOffset={setOffset} pageSize={CONV_PAGE} total={listed.length} unit="个" />
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
        <Button className="self-start" disabled={busy || !target.trim()} onClick={() => void preview()}>
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

export const MemoryPage = () => {
  const token = useSettingsStore(state => state.adminToken);
  const identity = useRef({ token, generation: 0 });
  if (identity.current.token !== token) identity.current = { token, generation: identity.current.generation + 1 };
  return <Fragment key={identity.current.generation}>
    <MemoryBlock />
    <ContextBlock />
    <ScopeBlock />
  </Fragment>;
};
