// 表情包库。上半页直接对图库动手，不走顶部那条 qq.yaml 状态条；下半页的收集设置才走。
import { useEffect, useRef, useState } from 'react';

import {
  acceptSticker,
  deleteSticker,
  discardSticker,
  importStickers,
  listStickers,
  recaptionStickers,
  stickerImageHref,
  updateSticker,
  uploadSticker,
  type Sticker,
  type StickerCounts,
  type StickerState,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Empty, Footnote, Grid, Option } from './components';
import { BoolOption, ChoiceField, NumberField, SubOption } from './fields';
import { sizeText } from './format';
import { IdList } from './IdList';

const PAGE_SIZE = 60;

const STATE_TABS: ReadonlyArray<readonly [StickerState, string]> = [
  ['pending', '待审'],
  ['library', '在库'],
  ['discarded', '已弃'],
];

const STRATEGY_CHOICES: ReadonlyArray<readonly [string, string]> = [
  ['strict', '严格'],
  ['loose', '宽松'],
  ['none', '全收'],
];

const CAPTION_TEXT: Record<Sticker['caption_state'], string> = {
  pending: '待打标',
  running: '打标中',
  done: '已打标',
  failed: '打标失败',
};

const EMPTY_COUNTS: StickerCounts = {
  pending: 0, library: 0, discarded: 0, usable: 0, awaiting_caption: 0,
};

const EMPTY_DRAFT = { name: '', tags: '', override: false };

const say = (error: unknown): string => (error instanceof Error ? error.message : '出错了');

const splitTags = (text: string): string[] =>
  text.split(/[,，]/).map((item) => item.trim()).filter(Boolean);

const LibraryBlock = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const [tab, setTab] = useState<StickerState>('pending');
  const [typed, setTyped] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<Sticker[]>([]);
  const [counts, setCounts] = useState<StickerCounts>(EMPTY_COUNTS);
  const [editing, setEditing] = useState('');
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [onlyFailed, setOnlyFailed] = useState(false);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  const [file, setFile] = useState<File | null>(null);
  const [uploadName, setUploadName] = useState('');
  const [uploadAccept, setUploadAccept] = useState(true);
  const [importPath, setImportPath] = useState('');
  const [importAccept, setImportAccept] = useState(false);
  const filePicker = useRef<HTMLInputElement>(null);

  const load = async () => {
    if (!adminToken) return;
    try {
      const page = await listStickers(adminToken, {
        state: tab, search, limit: PAGE_SIZE, offset,
      });
      setItems(page.items);
      setCounts(page.counts);
    } catch (error) { setNote(say(error)); }
  };

  useEffect(() => { void load(); }, [adminToken, tab, search, offset]);

  // 每敲一个字查一次会让 LIKE 全表扫打满，停手再查。
  useEffect(() => {
    const timer = setTimeout(() => { setSearch(typed.trim()); setOffset(0); }, 300);
    return () => { clearTimeout(timer); };
  }, [typed]);

  // 把最后一页删空之后要退回去，否则界面停在一片空白上。
  useEffect(() => {
    if (offset > 0 && items.length === 0) setOffset(Math.max(0, offset - PAGE_SIZE));
  }, [items, offset]);

  const run = async (job: (token: string) => Promise<string>) => {
    if (!adminToken || busy) return;
    setBusy(true);
    setNote('');
    try {
      const done = await job(adminToken);
      await load();
      setNote(done);
    } catch (error) {
      setNote(say(error));
    } finally {
      setBusy(false);
    }
  };

  const openEditor = (row: Sticker) => {
    if (editing === row.sha256) { setEditing(''); return; }
    setEditing(row.sha256);
    setDraft({
      name: row.name,
      tags: row.manual_tags.join('，'),
      override: row.manual_override,
    });
  };

  const saveEdit = (row: Sticker) => run(async (token) => {
    await updateSticker(token, row.sha256, {
      name: draft.name.trim(),
      manual_tags: splitTags(draft.tags),
      manual_override: draft.override,
    });
    setEditing('');
    return '已保存';
  });

  const accept = (row: Sticker) => run(async (token) => {
    await acceptSticker(token, row.sha256);
    return '已转入库';
  });

  const discard = (row: Sticker) => {
    const label = row.name || row.sha256.slice(0, 8);
    if (!window.confirm(`丢弃「${label}」？文件会删掉，哈希会记住，同一张图不会再被收第二次。`)) return;
    void run(async (token) => {
      await discardSticker(token, row.sha256);
      return '已丢弃';
    });
  };

  const forget = (row: Sticker) => {
    const label = row.name || row.sha256.slice(0, 8);
    if (!window.confirm(`彻底删除「${label}」？记录一起删，这张图重新出现在群里还会被收。`)) return;
    void run(async (token) => {
      await deleteSticker(token, row.sha256);
      return '已删除';
    });
  };

  const recaption = () => {
    const scope = onlyFailed ? '打标失败的图' : '全部在库与待审的图';
    if (!window.confirm(`把${scope}重新排进打标队列？打标要走视觉模型，量大时会占一阵子。`)) return;
    void run(async (token) => {
      const queued = await recaptionStickers(token, onlyFailed);
      return `已排队 ${queued} 张`;
    });
  };

  const upload = () => run(async (token) => {
    if (!file) return '先选一个文件';
    const row = await uploadSticker(token, file, { name: uploadName.trim(), accept: uploadAccept });
    setFile(null);
    setUploadName('');
    if (filePicker.current) filePicker.current.value = '';
    return `已上传「${row.name}」`;
  });

  const importDir = () => run(async (token) => {
    const result = await importStickers(token, importPath.trim(), importAccept);
    return `扫了 ${result.scanned} 个文件，收下 ${result.added} 张，`
      + `${result.known} 张已经有了，${result.skipped} 张不是能用的图片`;
  });

  const total = counts[tab];
  const from = items.length === 0 ? 0 : offset + 1;
  const to = offset + items.length;

  const card = (row: Sticker) => (
    <figure className="qc-stk" key={row.sha256}>
      <img
        alt={row.name || row.sha256.slice(0, 8)}
        className="qc-stk-img"
        loading="lazy"
        src={stickerImageHref(row.sha256, adminToken)}
      />
      <figcaption className="qc-stk-cap">
        <span className="qc-stk-name">{row.name || '未命名'}</span>
        <span className="qc-stk-meta">
          {row.width}×{row.height} · {sizeText(row.size)}
          {row.animated && ' · 动图'}
          {row.sent_count > 0 && ` · 发过 ${row.sent_count} 次`}
        </span>
        <span
          className={`qc-stk-flag${row.caption_state === 'failed' ? ' qc-stk-flag-halt' : ''}`}
          title={row.caption_error}
        >
          {CAPTION_TEXT[row.caption_state]}
        </span>

        {editing === row.sha256 ? (
          <div className="qc-stk-edit">
            <input
              className="qc-inp qc-inp-wide"
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              placeholder="名字，模型靠它点图"
              value={draft.name}
            />
            <input
              className="qc-inp qc-inp-wide"
              onChange={(event) => setDraft({ ...draft, tags: event.target.value })}
              placeholder="人工标签，逗号分隔"
              value={draft.tags}
            />
            <label className="qc-check">
              <input
                checked={draft.override}
                onChange={(event) => setDraft({ ...draft, override: event.target.checked })}
                type="checkbox"
              />
              <span>人工覆盖自动标签</span>
            </label>
            <div className="qc-stk-acts">
              <button className="qc-btn" disabled={busy} onClick={() => void saveEdit(row)} type="button">
                保存
              </button>
              <button className="qc-btn qc-btn-quiet" onClick={() => setEditing('')} type="button">
                取消
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="qc-chips qc-stk-tags">
              {row.tags.length === 0
                ? <span className="qc-chip-empty">还没有标签</span>
                : row.tags.map((word) => <span className="qc-chip" key={word}>{word}</span>)}
            </div>
            <div className="qc-stk-acts">
              <button className="qc-btn qc-btn-quiet" onClick={() => openEditor(row)} type="button">
                编辑
              </button>
              {row.state === 'pending' && (
                <button className="qc-btn" disabled={busy} onClick={() => void accept(row)} type="button">
                  转正
                </button>
              )}
              {row.state !== 'discarded' && (
                <button className="qc-btn qc-btn-quiet" disabled={busy} onClick={() => discard(row)} type="button">
                  丢弃
                </button>
              )}
              <button className="qc-btn qc-btn-danger" disabled={busy} onClick={() => forget(row)} type="button">
                删除
              </button>
            </div>
          </>
        )}
      </figcaption>
    </figure>
  );

  return (
    <>
      <Block hint="待审的转正之后模型才挑得到；丢弃会记住哈希，彻底删不会" title="图库">
        <p className="qc-facts">
          在库 {counts.library} 张，其中 {counts.usable} 张打过标能用 ·
          待审 {counts.pending} 张 · 已弃 {counts.discarded} 张 ·
          还有 {counts.awaiting_caption} 张没打标
        </p>

        <div className="qc-stk-bar">
          <div className="qc-grants" role="group">
            {STATE_TABS.map(([key, label]) => (
              <button
                aria-pressed={key === tab}
                className={`qc-grant${key === tab ? ' qc-grant-on' : ''}`}
                key={key}
                onClick={() => { setTab(key); setOffset(0); setEditing(''); }}
                type="button"
              >
                {label} {counts[key]}
              </button>
            ))}
          </div>
          <input
            className="qc-inp"
            onChange={(event) => setTyped(event.target.value)}
            placeholder="搜名字或标签"
            value={typed}
          />
          <span className="qc-bar-spacer" />
          <label className="qc-check">
            <input
              checked={onlyFailed}
              onChange={(event) => setOnlyFailed(event.target.checked)}
              type="checkbox"
            />
            <span>只重试失败的</span>
          </label>
          <button className="qc-btn qc-btn-quiet" disabled={busy} onClick={recaption} type="button">
            重新打标
          </button>
        </div>

        {items.length === 0 ? (
          <Empty>{search ? '没有匹配的图。' : '这一档还是空的。'}</Empty>
        ) : (
          <div className="qc-stk-grid">{items.map(card)}</div>
        )}

        {total > PAGE_SIZE && (
          <div className="qc-stk-page">
            <span className="qc-stk-meta">第 {from}–{to} 张，共 {total} 张</span>
            <span className="qc-bar-spacer" />
            <button
              className="qc-btn qc-btn-quiet"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              type="button"
            >
              上一页
            </button>
            <button
              className="qc-btn qc-btn-quiet"
              disabled={to >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
              type="button"
            >
              下一页
            </button>
          </div>
        )}
        {note && <Footnote>{note}</Footnote>}
      </Block>

      <Block hint="上传与导入立刻落盘，不用点顶部的应用" title="添加">
        <div className="qc-panel">
          <div className="qc-chan-row">
            <span className="qc-chan-label">文件</span>
            <div className="qc-chan-body">
              <input
                accept="image/*"
                className="qc-inp"
                onChange={(event) => setFile(event.target.files?.[0] || null)}
                ref={filePicker}
                type="file"
              />
              <button
                className="qc-btn"
                disabled={busy || !file}
                onClick={() => void upload()}
                type="button"
              >
                上传
              </button>
            </div>
          </div>
          <div className="qc-chan-row">
            <span className="qc-chan-label">名字</span>
            <div className="qc-chan-body">
              <input
                className="qc-inp"
                onChange={(event) => setUploadName(event.target.value)}
                placeholder="留空就用文件名"
                value={uploadName}
              />
              <label className="qc-check">
                <input
                  checked={uploadAccept}
                  onChange={(event) => setUploadAccept(event.target.checked)}
                  type="checkbox"
                />
                <span>直接入库</span>
              </label>
            </div>
          </div>

          <div className="qc-chan-row">
            <span className="qc-chan-label">目录</span>
            <div className="qc-chan-body">
              <input
                className="qc-inp"
                onChange={(event) => setImportPath(event.target.value)}
                placeholder="工作区里的相对路径，例如 data/stickers-inbox"
                value={importPath}
              />
              <label className="qc-check">
                <input
                  checked={importAccept}
                  onChange={(event) => setImportAccept(event.target.checked)}
                  type="checkbox"
                />
                <span>直接入库</span>
              </label>
              <button
                className="qc-btn"
                disabled={busy || !importPath.trim()}
                onClick={() => void importDir()}
                type="button"
              >
                导入
              </button>
            </div>
          </div>
          <p className="qc-facts">
            目录必须在工作区内，会连子目录一起扫。单张超过 8 MB 或认不出格式的会被跳过。
          </p>
        </div>
      </Block>
    </>
  );
};

const CollectBlock = () => (
  <Block hint="改完点顶部的应用，bot 读到之后才生效" title="收集设置">
    <Grid>
      <BoolOption
        configKey="sticker_collect"
        desc="把群里刷过的表情包攒进图库。"
        label="收集表情包"
      >
        <p className="qc-opt-desc">
          严格只收 QQ 标了表情包的，宽松再放行看着不像截图的，全收连群友随手发的照片都要。
        </p>
        <ChoiceField
          choices={STRATEGY_CHOICES}
          configKey="sticker_strategy"
          fallback="loose"
          label="收多严"
        />
        <SubOption configKey="sticker_auto_accept" label="收到就直接入库，跳过人工筛选" />
        <IdList
          configKey="sticker_groups"
          empty="所有已授权的群都收"
          label="只从这些群收"
          placeholder="输入群号，回车添加"
        />
      </BoolOption>

      <Option checked disabled name="容量" onChange={() => undefined}>
        <p className="qc-opt-desc">待审池满了就不再收新的；图库超出上限时最旧的先出局。</p>
        <NumberField configKey="sticker_pending_limit" label="待审池" step={10} unit="张" />
        <NumberField configKey="sticker_library_limit" label="图库" step={50} unit="张" />
        <NumberField configKey="sticker_pending_ttl_days" label="待审保留" unit="天" />
      </Option>

      <Option checked disabled name="发图" onChange={() => undefined}>
        <p className="qc-opt-desc">候选给多了是 token 炸弹，给少了模型挑不出合适的。</p>
        <NumberField configKey="sticker_prompt_limit" label="每轮候选" unit="张" />
        <NumberField configKey="sticker_repeat_window_sec" label="同图间隔" step={60} unit="秒" />
      </Option>

      <BoolOption
        configKey="sticker_combat"
        desc="群里连着刷图时跟着接一张。要求连图来自至少两个人，中间夹了纯文本就不算。"
        label="接梗"
      >
        <NumberField
          configKey="sticker_burst_probability"
          label="发完再补一张的概率"
          max={1}
          step={0.05}
        />
      </BoolOption>
    </Grid>
  </Block>
);

export const StickersPage = () => (
  <>
    <LibraryBlock />
    <CollectBlock />
    <Footnote>
      只有在库且打过标的图才会进候选 —— 没打标的图对模型来说是不可描述的，给了也挑不出来。
      打标走视觉模型，跟着 bot 进程慢慢跑，刚收进来的图要等一会儿才可用。
    </Footnote>
  </>
);
