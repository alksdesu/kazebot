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
import {
  Block,
  Button,
  Check,
  Chip,
  Desc,
  Empty,
  Facts,
  Footnote,
  Grid,
  Input,
  Option,
  Panel,
  Segmented,
} from './components';
import { BoolOption, ChoiceField, NumberField, SubOption } from './fields';
import { sizeText } from './format';
import { IdList } from './IdList';
import { VisionChannelBlock } from './VisionChannelBlock';

const PAGE_SIZE = 60;

const META = 'text-[0.65rem] text-[var(--duties-tertiary)]';

const ACTS = 'flex flex-wrap gap-1';

const CHAN_ROW = 'mt-1.5 flex items-center gap-2.5';
const CHAN_LABEL = 'flex-none basis-[3em] text-xs text-[var(--duties-secondary)]';
const CHAN_BODY = 'flex min-w-0 flex-1 items-center gap-2';

// 文件选择框拿 ref 清值，函数组件的 Input 传不进 ref，只能自己披皮。
const FILE_PICKER =
  'min-w-0 flex-1 border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1'
  + ' font-mono text-xs text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';

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
    <figure
      className="m-0 flex flex-col gap-1.5 border border-[var(--duties-border)] bg-[var(--duties-panel)] p-2"
      key={row.sha256}
    >
      {/* 表情包长宽参差，定高加 contain 才对得成网格；底色衬出透明图的边界。 */}
      <img
        alt={row.name || row.sha256.slice(0, 8)}
        className="h-[7.25rem] w-full bg-[var(--duties-muted)] object-contain"
        loading="lazy"
        src={stickerImageHref(row.sha256, adminToken)}
      />
      <figcaption className="flex min-w-0 flex-col gap-1">
        <span className="truncate text-xs font-medium">{row.name || '未命名'}</span>
        <span className={META}>
          {row.width}×{row.height} · {sizeText(row.size)}
          {row.animated && ' · 动图'}
          {row.sent_count > 0 && ` · 发过 ${row.sent_count} 次`}
        </span>
        <span
          className={`self-start border px-1.5 text-[0.65rem] ${
            row.caption_state === 'failed'
              ? 'border-[var(--duties-danger)] text-[var(--duties-danger)]'
              : 'border-[var(--duties-border)] text-[var(--duties-secondary)]'
          }`}
          title={row.caption_error}
        >
          {CAPTION_TEXT[row.caption_state]}
        </span>

        {editing === row.sha256 ? (
          <div className="flex flex-col gap-1.5">
            <Input
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              placeholder="名字，模型靠它点图"
              value={draft.name}
              width="wide"
            />
            <Input
              onChange={(event) => setDraft({ ...draft, tags: event.target.value })}
              placeholder="人工标签，逗号分隔"
              value={draft.tags}
              width="wide"
            />
            <Check
              checked={draft.override}
              onChange={(checked) => setDraft({ ...draft, override: checked })}
            >
              人工覆盖自动标签
            </Check>
            <div className={ACTS}>
              <Button disabled={busy} onClick={() => void saveEdit(row)} size="sm">
                保存
              </Button>
              <Button onClick={() => setEditing('')} size="sm" tone="quiet">
                取消
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap gap-1.5">
              {row.tags.length === 0
                ? <span className="text-xs text-[var(--duties-secondary)]">还没有标签</span>
                : row.tags.map((word) => <Chip key={word}>{word}</Chip>)}
            </div>
            <div className={ACTS}>
              <Button onClick={() => openEditor(row)} size="sm" tone="quiet">
                编辑
              </Button>
              {row.state === 'pending' && (
                <Button disabled={busy} onClick={() => void accept(row)} size="sm">
                  转正
                </Button>
              )}
              {row.state !== 'discarded' && (
                <Button disabled={busy} onClick={() => discard(row)} size="sm" tone="quiet">
                  丢弃
                </Button>
              )}
              <Button disabled={busy} onClick={() => forget(row)} size="sm" tone="danger">
                删除
              </Button>
            </div>
          </>
        )}
      </figcaption>
    </figure>
  );

  return (
    <>
      <Block hint="待审的转正之后模型才挑得到；丢弃会记住哈希，彻底删不会" title="图库">
        <Facts>
          在库 {counts.library} 张，其中 {counts.usable} 张打过标能用 ·
          待审 {counts.pending} 张 · 已弃 {counts.discarded} 张 ·
          还有 {counts.awaiting_caption} 张没打标
        </Facts>

        <div className="my-3 flex flex-wrap items-center gap-2">
          <Segmented
            choices={STATE_TABS.map(([key, label]) => [key, `${label} ${counts[key]}`] as const)}
            onPick={(key) => { setTab(key); setOffset(0); setEditing(''); }}
            value={tab}
          />
          <Input
            onChange={(event) => setTyped(event.target.value)}
            placeholder="搜名字或标签"
            value={typed}
            width="flex"
          />
          <span className="flex-1" />
          <Check checked={onlyFailed} onChange={setOnlyFailed}>只重试失败的</Check>
          <Button disabled={busy} onClick={recaption} tone="quiet">
            重新打标
          </Button>
        </div>

        {items.length === 0 ? (
          <Empty>{search ? '没有匹配的图。' : '这一档还是空的。'}</Empty>
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(11rem,1fr))] gap-2.5">
            {items.map(card)}
          </div>
        )}

        {total > PAGE_SIZE && (
          <div className="mt-3 flex items-center gap-2">
            <span className={META}>第 {from}–{to} 张，共 {total} 张</span>
            <span className="flex-1" />
            <Button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              tone="quiet"
            >
              上一页
            </Button>
            <Button
              disabled={to >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
              tone="quiet"
            >
              下一页
            </Button>
          </div>
        )}
        {note && <Footnote>{note}</Footnote>}
      </Block>

      <Block hint="上传与导入立刻落盘，不用点顶部的应用" title="添加">
        <Panel>
          <div className={CHAN_ROW}>
            <span className={CHAN_LABEL}>文件</span>
            <div className={CHAN_BODY}>
              <input
                accept="image/*"
                className={FILE_PICKER}
                onChange={(event) => setFile(event.target.files?.[0] || null)}
                ref={filePicker}
                type="file"
              />
              <Button
                className="flex-none"
                disabled={busy || !file}
                onClick={() => void upload()}
              >
                上传
              </Button>
            </div>
          </div>
          <div className={CHAN_ROW}>
            <span className={CHAN_LABEL}>名字</span>
            <div className={CHAN_BODY}>
              <Input
                onChange={(event) => setUploadName(event.target.value)}
                placeholder="留空就用文件名"
                value={uploadName}
                width="flex"
              />
              <Check checked={uploadAccept} onChange={setUploadAccept}>直接入库</Check>
            </div>
          </div>

          <div className={CHAN_ROW}>
            <span className={CHAN_LABEL}>目录</span>
            <div className={CHAN_BODY}>
              <Input
                onChange={(event) => setImportPath(event.target.value)}
                placeholder="工作区里的相对路径，例如 data/stickers-inbox"
                value={importPath}
                width="flex"
              />
              <Check checked={importAccept} onChange={setImportAccept}>直接入库</Check>
              <Button
                className="flex-none"
                disabled={busy || !importPath.trim()}
                onClick={() => void importDir()}
              >
                导入
              </Button>
            </div>
          </div>
          <Facts>
            目录必须在工作区内，会连子目录一起扫。单张超过 8 MB 或认不出格式的会被跳过。
          </Facts>
        </Panel>
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
        <Desc>
          严格只收 QQ 标了表情包的，宽松再放行看着不像截图的，全收连群友随手发的照片都要。
        </Desc>
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
        <Desc>待审池满了就不再收新的；图库超出上限时最旧的先出局。</Desc>
        <NumberField configKey="sticker_pending_limit" label="待审池" step={10} unit="张" />
        <NumberField configKey="sticker_library_limit" label="图库" step={50} unit="张" />
        <NumberField configKey="sticker_pending_ttl_days" label="待审保留" unit="天" />
      </Option>

      <Option checked disabled name="发图" onChange={() => undefined}>
        <Desc>候选给多了是 token 炸弹，给少了模型挑不出合适的。</Desc>
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
    <VisionChannelBlock />
    <CollectBlock />
    <Footnote>
      只有在库且打过标的图才会进候选 —— 没打标的图对模型来说是不可描述的，给了也挑不出来。
      打标走上面那条看图渠道，跟着 bot 进程慢慢跑，刚收进来的图要等一会儿才可用；
      渠道没配好时打标会整个停用，「还有多少没打标」就一直降不下去。
    </Footnote>
  </>
);
