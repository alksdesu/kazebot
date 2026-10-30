// 表情包图库。这台机器上的实例共用一份：图的增删改立即写入，入库规则按块保存进全局设置；平台自己的收集与发送规则在平台页里。
import { useEffect, useRef, useState } from 'react';

import {
  acceptSticker,
  deleteSticker,
  discardSticker,
  importStickers,
  listStickers,
  recaptionStickers,
  receipted,
  stickerImageUrl,
  updateSticker,
  uploadSticker,
  type Sticker,
  type StickerCounts,
  type StickerState,
} from '../api/supervisorClient';
import { AuthorizedImage } from '../components/common/AuthorizedImage';
import { usePageNotice } from '../hooks/usePageNotice';
import { useRequestGuard } from '../hooks/useRequestGuard';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';
import {
  Block,
  Button,
  Check,
  Chip,
  Empty,
  ErrorText,
  Facts,
  Footnote,
  Input,
  Pager,
  Panel,
  Segmented,
} from './components';
import { StandaloneFeatureSection, STICKER_LIBRARY_SECTION } from './FeatureSection';
import { sizeText } from './format';
import { VisionChannelBlock } from './VisionChannelBlock';
import { errorMessage } from '../api/request';
import { appendSentence } from '../utils/sentence';

const PAGE_SIZE = 60;

const META = 'text-mini text-[var(--duties-tertiary)]';

const ACTS = 'flex flex-wrap gap-1';

const CHAN_ROW = 'mt-1.5 flex items-center gap-2.5';
const CHAN_LABEL = 'flex-none basis-[var(--duties-count-width)] text-xs text-[var(--duties-secondary)]';
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

const CAPTION_TEXT: Record<Sticker['caption_state'], string> = {
  pending: '待打标',
  running: '打标中',
  done: '已打标',
  failed: '打标失败',
};

/** 操作做成的那句。回执读不懂时改动也已经做完，receiptProblem 写着哪里没读懂。 */
interface JobOutcome {
  done: string;
  receiptProblem?: string;
}

const EMPTY_DRAFT = { name: '', tags: '', override: false };

const splitTags = (text: string): string[] =>
  text.split(/[,，]/).map((item) => item.trim()).filter(Boolean);

const LibraryBlock = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const [tab, setTab] = useState<StickerState>('pending');
  const [typed, setTyped] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<Sticker[]>([]);
  // 这一页没读成时手上的图可能是上一个页签、上一次搜索留下的：不摆出来，也不说「还没有图」
  const [listFailed, setListFailed] = useState(false);
  // 一次都没读成时是 null：各档摆成 0 张，就是说图库是空的。
  const [counts, setCounts] = useState<StickerCounts | null>(null);
  const [editing, setEditing] = useState('');
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [editBaseline, setEditBaseline] = useState('');
  const [onlyFailed, setOnlyFailed] = useState(false);
  const { notice, error, setMessage, setError, setLoadError } = usePageNotice();
  const [busy, setBusy] = useState(false);

  const [file, setFile] = useState<File | null>(null);
  const [uploadName, setUploadName] = useState('');
  const [uploadAccept, setUploadAccept] = useState(true);
  const [importPath, setImportPath] = useState('');
  const [importAccept, setImportAccept] = useState(false);
  const filePicker = useRef<HTMLInputElement>(null);
  const requests = useRequestGuard();

  const confirmDiscard = useUnsavedChanges(
    editing !== '' && JSON.stringify(draft) !== editBaseline,
    '「图库」里有一张图的名字或标签还没保存，确定离开或丢弃吗？',
  );

  const load = async () => {
    if (!adminToken) return;
    // 切页签、翻页、搜索接连触发时只认最后发出的那次，慢回来的旧结果不能盖掉眼前这一页。
    const request = requests.begin('page');
    try {
      const page = await listStickers(adminToken, {
        state: tab, search, limit: PAGE_SIZE, offset,
      });
      if (!request.isCurrent()) return;
      setItems(page.items);
      setCounts(page.counts);
      setListFailed(false);
      setLoadError('');
    } catch (caught) {
      if (!request.isCurrent()) return;
      setListFailed(true);
      setLoadError(errorMessage(caught));
    }
  };

  useEffect(() => { void load(); }, [requests, tab, search, offset]);

  // 每敲一个字查一次会让 LIKE 全表扫打满，停手再查。
  useEffect(() => {
    const timer = setTimeout(() => { setSearch(typed.trim()); setOffset(0); }, 300);
    return () => { clearTimeout(timer); };
  }, [typed]);

  // 把最后一页删空之后要退回去，否则界面停在一片空白上。
  useEffect(() => {
    if (offset > 0 && items.length === 0) setOffset(Math.max(0, offset - PAGE_SIZE));
  }, [items, offset]);

  const run = async (job: (token: string) => Promise<JobOutcome>) => {
    if (!adminToken || busy) return;
    setBusy(true);
    setMessage('');
    try {
      const { done, receiptProblem = '' } = await job(adminToken);
      if (!requests.isCurrent()) return;
      await load();
      if (!requests.isCurrent()) return;
      setMessage(done, receiptProblem);
    } catch (caught) {
      if (!requests.isCurrent()) return;
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const openEditor = (row: Sticker) => {
    if (editing === row.sha256) { if (confirmDiscard()) setEditing(''); return; }
    if (!confirmDiscard()) return;
    const opened = {
      name: row.name,
      tags: row.manual_tags.join('，'),
      override: row.manual_override,
    };
    setEditing(row.sha256);
    setDraft(opened);
    setEditBaseline(JSON.stringify(opened));
  };

  const saveEdit = (row: Sticker) => run(async (token) => {
    const { receiptProblem } = await receipted(updateSticker(token, row.sha256, {
      name: draft.name.trim(),
      manual_tags: splitTags(draft.tags),
      manual_override: draft.override,
    }));
    setEditing('');
    return { done: '已保存', receiptProblem };
  });

  const accept = (row: Sticker) => run(async (token) => {
    const { receiptProblem } = await receipted(acceptSticker(token, row.sha256));
    return { done: '已转正，进候选了', receiptProblem };
  });

  const discard = (row: Sticker) => {
    const label = row.name || row.sha256.slice(0, 8);
    if (!window.confirm(`丢弃「${label}」？文件会删掉，哈希会记住，同一张图不会再被收第二次。`)) return;
    void run(async (token) => {
      await discardSticker(token, row.sha256);
      return { done: '已丢弃' };
    });
  };

  const forget = (row: Sticker) => {
    const label = row.name || row.sha256.slice(0, 8);
    if (!window.confirm(`彻底删除「${label}」？记录一起删，这张图重新出现在群里还会被收。`)) return;
    void run(async (token) => {
      await deleteSticker(token, row.sha256);
      return { done: '已删除' };
    });
  };

  const recaption = () => {
    const scope = onlyFailed ? '打标失败的图' : '全部在库与待审的图';
    if (!window.confirm(`把${scope}重新排进打标队列？打标要走视觉模型，量大时会占一阵子。`)) return;
    void run(async (token) => {
      const { result: queued, receiptProblem } = await receipted(recaptionStickers(token, onlyFailed));
      return queued === null
        ? { done: '已重新排进打标队列，打标在后台跑', receiptProblem: appendSentence(receiptProblem, '排了几张没读到') }
        : { done: `已排队 ${queued} 张，打标在后台跑` };
    });
  };

  const upload = () => run(async (token) => {
    if (!file) throw new Error('先选一个文件');
    const { result: row, receiptProblem } = await receipted(
      uploadSticker(token, file, { name: uploadName.trim(), accept: uploadAccept }),
    );
    // 回执读不懂时图也已经传上了：照传完清掉选择，免得人再传一次撞上「库里已经有了」。
    setFile(null);
    setUploadName('');
    if (filePicker.current) filePicker.current.value = '';
    return { done: row ? `已上传「${row.name}」` : '已上传', receiptProblem };
  });

  const importDir = () => run(async (token) => {
    const { result, receiptProblem } = await receipted(importStickers(token, importPath.trim(), importAccept));
    if (!result) {
      return { done: '已导入', receiptProblem: appendSentence(receiptProblem, '收下几张没读到，上面的张数以重新读到的为准') };
    }
    return {
      done: `扫了 ${result.scanned} 个文件，收下 ${result.added} 张，`
        + `${result.known} 张已经有了，${result.skipped} 张不是能用的图片`,
    };
  });

  const total = counts ? counts[tab] : 0;

  const card = (row: Sticker) => (
    <figure
      className="m-0 flex flex-col gap-1.5 border border-[var(--duties-border)] bg-[var(--duties-panel)] p-2"
      key={row.sha256}
    >
      {/* 表情包长宽参差，定高加 contain 才对得成网格；底色衬出透明图的边界。 */}
      <AuthorizedImage
        alt={row.name || row.sha256.slice(0, 8)}
        className="h-[var(--duties-thumb-height)] w-full bg-[var(--duties-muted)] object-contain"
        lazy
        source={stickerImageUrl(row.sha256)}
      />
      <figcaption className="flex min-w-0 flex-col gap-1">
        <span className="truncate text-xs font-medium">{row.name || '未命名'}</span>
        <span className={META}>
          {row.width}×{row.height} · {sizeText(row.size)}
          {row.animated && ' · 动图'}
          {row.sent_count > 0 && ` · 发过 ${row.sent_count} 次`}
        </span>
        <span
          className={`self-start border px-1.5 text-mini ${
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
              <Button onClick={() => { if (confirmDiscard()) setEditing(''); }} size="sm" tone="quiet">
                丢弃
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
          {counts ? (
            <>
              在库 {counts.library} 张，其中 {counts.usable} 张打过标能用 ·
              待审 {counts.pending} 张 · 已弃 {counts.discarded} 张 ·
              还有 {counts.awaiting_caption} 张没打标
            </>
          ) : listFailed ? '各档的张数没读到，原因见下。' : '正在读取图库…'}
        </Facts>

        <div className="my-3 flex flex-wrap items-center gap-2">
          <Segmented
            choices={STATE_TABS.map(([key, label]) => [key, counts ? `${label} ${counts[key]}` : label] as const)}
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

        {listFailed && (
          <Button className="mb-2" disabled={busy} onClick={() => void load()} tone="quiet">重新读取这一页</Button>
        )}
        {!listFailed && (items.length === 0 ? (
          <Empty>{search ? '没有名字或标签含「' + search + '」的图。' : '这一档还没有图。'}</Empty>
        ) : (
          <div className="grid grid-cols-[var(--duties-thumb-grid)] gap-2.5">
            {items.map(card)}
          </div>
        ))}

        <Pager offset={offset} onOffset={setOffset} pageSize={PAGE_SIZE} total={total} unit="张" />
        {error && <ErrorText role="alert">{error}</ErrorText>}
        {notice && <Footnote>{notice}</Footnote>}
      </Block>

      <Block hint="上传和导入都是立刻写入" title="添加">
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
        </Panel>

        <Panel className="mt-2.5">
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

export const StickersPage = () => (
  <>
    <LibraryBlock />
    {/* 入库规则写的是全局设置文件，和上面立刻执行的操作不同，改完要点这一块自己的保存。 */}
    <StandaloneFeatureSection leaveMessage="「入库」里还有没保存的修改，确定离开吗？" sectionId={STICKER_LIBRARY_SECTION} />
    <VisionChannelBlock />
    <Footnote>
      只有在库且打过标的图才会进候选 —— 没打标的图对模型来说是不可描述的，给了也挑不出来。
      打标走上面显示的那条看图渠道，在后台慢慢跑，bot 不在线也照打，刚收进来的图要等一会儿才可用；
      渠道没配好时打标会整个停用，「还有多少没打标」就一直降不下去。
    </Footnote>
  </>
);
