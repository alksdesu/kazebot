// 控制台各页在设置侧栏里的登记项。页面本体只写内容，外壳由这里统一套：
// 平台页走 PlatformPage（状态栏 + 连接卡 + 折叠分区），机器人页与模型页走通用素壳。
import { AccountPage } from './AccountPage';
import { DiagnosticsPanel } from '../features/operations/DiagnosticsPanel';
import { ChannelsPage } from './ChannelsPage';
import { PlainPage } from './components';
import { ConnectionCard } from './ConnectionCard';
import { InputPage } from './InputPage';
import { InteractionPage } from './InteractionPage';
import { MemoryPage } from './MemoryPage';
import { ModelsPage } from './ModelsPage';
import { OutputPage } from './OutputPage';
import { PermissionsPage } from './PermissionsPage';
import { PersonaPage } from './PersonaPage';
import { PlatformPage, type PlatformSection } from './PlatformPage';
import { ProvidersPage } from './ProvidersPage';
import { RuntimePage } from './RuntimePage';
import { StickerCollectPage } from './StickerCollectPage';
import { StickersPage } from './StickersPage';
import { TimingPage } from './TimingPage';
import { QQ_NAPCAT, type PlatformDescriptor } from '../platforms/registry';
import {
  AccountSummary,
  ChannelsSummary,
  InputSummary,
  InteractionSummary,
  LinkSummary,
  OutputSummary,
  PermissionsSummary,
  StickerSummary,
  TriggerSummary,
} from './sectionSummaries';

export interface ConsolePageDefinition {
  /** 设置页 tab id。改名要在 settingsTabAliases 里留一条别名，老书签才不会失效。 */
  id: string;
  label: string;
  icon: string;
  note: string;
  Page: () => JSX.Element;
}

/** 全平台共用的那几页：它是谁、记得什么、图库里有什么。 */
export const BOT_PAGES: ConsolePageDefinition[] = [
  {
    id: 'persona',
    label: '人格',
    icon: 'draft',
    Page: PersonaPage,
    note: 'bot 是谁、用哪个模型。两块各自保存，写的是节点文件，所有平台共用同一份。',
  },
  {
    id: 'memory',
    label: '记忆',
    icon: 'psychology',
    Page: MemoryPage,
    note: 'bot 记住了什么、每个会话攒了多少上下文。改动立即写入。',
  },
  {
    id: 'stickers',
    label: '表情包图库',
    icon: 'photo_library',
    Page: StickersPage,
    note: '全平台共用一份图库。群里收来的先进待审，人工转正之后才进候选；每块自己保存。',
  },
];

/** 模型与渠道。写的是 data/config.yaml，和任何平台都无关。 */
export const MODEL_PAGES: ConsolePageDefinition[] = [
  {
    id: 'providers',
    label: '渠道',
    icon: 'hub',
    Page: ProvidersPage,
    note: 'bot 用哪一家的模型，以及它的地址和密钥。改完下一条消息就生效，不用重启。',
  },
  {
    id: 'models',
    label: '模型',
    icon: 'model_training',
    Page: ModelsPage,
    note: '发给模型的请求里带哪些参数。清单由各家 provider 自己声明，全局与节点分两层，节点优先。',
  },
];

/** QQ · NapCat 的分区。paths 对着 bot 公布的键路径，决定「N 项待应用」算在谁头上。 */
export const QQ_SECTIONS: PlatformSection[] = [
  {
    id: 'account',
    label: '账号与连接',
    icon: 'account_circle',
    paths: [],
    Summary: AccountSummary,
    Body: AccountPage,
    note: '这一个实例用哪个号在说话，以及多开与记忆归属。换号会重启 NapCat，期间 bot 不可用。',
  },
  {
    id: 'channels',
    label: '信道',
    icon: 'inbox',
    paths: ['channels'],
    Summary: ChannelsSummary,
    Body: ChannelsPage,
    note: 'bot 只在名单里的群和私聊里出现。两份名单都是白名单，不在名单里的消息连处理都不会处理。',
  },
  {
    id: 'trigger',
    label: '触发时机',
    icon: 'timer',
    paths: ['trigger'],
    Summary: TriggerSummary,
    Body: TimingPage,
    note: '决定 bot 在群里什么时候说话。多个条件之间是「或」——命中任意一条就会回应，再由冷却决定是否真的开口。',
  },
  {
    id: 'permissions',
    label: '权限',
    icon: 'verified_user',
    paths: ['permissions', 'approval'],
    Summary: PermissionsSummary,
    Body: PermissionsPage,
    note: '谁能对 bot 下管理命令。管理员权限跨所有群和私聊生效，不区分场景。',
  },
  {
    id: 'input',
    label: '输入处理',
    icon: 'attach_file',
    paths: ['input'],
    Summary: InputSummary,
    Body: InputPage,
    note: '群友发来的图片、文件和合并转发，哪些收、收多少。',
  },
  {
    id: 'output',
    label: '输出格式',
    icon: 'format_align_left',
    paths: ['output', 'extensions.reactions'],
    Summary: OutputSummary,
    Body: OutputPage,
    note: '消息发出去之前的处理：分条、去掉 Markdown 标记、长度上限、表态。',
  },
  {
    id: 'interaction',
    label: '群互动',
    icon: 'chat',
    // auto_like_times 和 auto_like 是同级键，不是它的子路径，得各列一条。
    paths: ['extensions.echo', 'extensions.preempt', 'extensions.auto_like', 'extensions.auto_like_times'],
    Summary: InteractionSummary,
    Body: InteractionPage,
    note: '除了说话之外还能做什么：跟复读、抢答、给资料卡点赞。',
  },
  {
    id: 'sticker',
    label: '表情包',
    icon: 'mood',
    paths: ['extensions.sticker'],
    Summary: StickerSummary,
    Body: StickerCollectPage,
    note: '在 QQ 这一侧怎么收表情包、怎么发。图库本身在「机器人 › 表情包图库」里管。',
  },
  {
    id: 'link',
    label: '链路',
    icon: 'lan',
    paths: ['queue', 'history', 'forward_bridge'],
    Summary: LinkSummary,
    Body: RuntimePage,
    note: '队列、群历史、转发桥这几个改完立刻重建运行期对象的参数，以及固定配置与危险操作。',
  },
];

export interface PlatformPageDefinition {
  id: string;
  label: string;
  icon: string;
  note: string;
  platform: PlatformDescriptor;
  sections: PlatformSection[];
}

export const PLATFORM_PAGES: PlatformPageDefinition[] = [
  {
    id: 'platform-qq',
    label: QQ_NAPCAT.displayName,
    icon: 'inbox',
    platform: QQ_NAPCAT,
    sections: QQ_SECTIONS,
    note: 'bot 在 QQ 上怎么收发消息。改完点顶部的「应用」写进配置文件，状态条会显示 bot 有没有读到这一份。',
  },
];

/** 一个平台一页。分区在页内折叠，深链靠 ?section= 定位。 */
export function wrapPlatformPage({ note, platform, sections }: PlatformPageDefinition): () => JSX.Element {
  const Wrapped = () => (
    <PlatformPage
      connection={<><ConnectionCard /><DiagnosticsPanel /></>}
      note={note}
      platform={platform}
      sections={sections}
    />
  );
  Wrapped.displayName = `PlatformPage(${platform.displayName})`;
  return Wrapped;
}

/** 把不碰平台配置的页包进通用页壳。 */
export function wrapConsolePage(eyebrow: string, { label, note, Page }: ConsolePageDefinition): () => JSX.Element {
  const Wrapped = () => (
    <PlainPage eyebrow={eyebrow} note={note} title={label}>
      <Page />
    </PlainPage>
  );
  Wrapped.displayName = `ConsolePage(${label})`;
  return Wrapped;
}
