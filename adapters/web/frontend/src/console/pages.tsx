// 控制台各页在设置侧栏里的登记项。页面本体只写内容，外壳由这里统一套：
// 平台页走 QqPage（带生效状态条），机器人页与模型页不碰平台配置，走通用页壳。
import { AccountPage } from './AccountPage';
import { DiagnosticsPanel } from '../features/operations/DiagnosticsPanel';
import { ChannelsPage } from './ChannelsPage';
import { PlainPage, QqPage } from './components';
import { InputPage } from './InputPage';
import { InteractionPage } from './InteractionPage';
import { MemoryPage } from './MemoryPage';
import { ModelsPage } from './ModelsPage';
import { OutputPage } from './OutputPage';
import { PermissionsPage } from './PermissionsPage';
import { PersonaPage } from './PersonaPage';
import { ProvidersPage } from './ProvidersPage';
import { RuntimePage } from './RuntimePage';
import { StickerCollectPage } from './StickerCollectPage';
import { StickersPage } from './StickersPage';
import { TimingPage } from './TimingPage';

export interface ConsolePageDefinition {
  /** 设置页 tab id。改名要在 settingsTabAliases 里留一条别名，老书签才不会失效。 */
  id: string;
  label: string;
  icon: string;
  note: string;
  Page: () => JSX.Element;
  AlwaysPanel?: () => JSX.Element;
}

export interface PlatformSectionDefinition extends ConsolePageDefinition {
  /** 写的不是平台配置文件。bot 没跑也该能改，不等它公布生效配置。 */
  standalone?: boolean;
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

/** QQ · NapCat 的分区。顺序即界面顺序，与 qq.yaml 的配置段一一对应。 */
export const QQ_SECTIONS: PlatformSectionDefinition[] = [
  {
    id: 'qq-account',
    label: '账号与连接',
    icon: 'account_circle',
    Page: AccountPage,
    standalone: true,
    note: '这一个实例用哪个号在说话、链路通没通，以及多开与记忆归属。换号会重启 NapCat，期间 bot 不可用。',
  },
  {
    id: 'qq-channels',
    label: '信道',
    icon: 'inbox',
    Page: ChannelsPage,
    note: 'bot 只在名单里的群和私聊里出现。两份名单都是白名单，不在名单里的消息连处理都不会处理。',
  },
  {
    id: 'qq-trigger',
    label: '触发时机',
    icon: 'timer',
    Page: TimingPage,
    note: '决定 bot 在群里什么时候说话。多个条件之间是「或」——命中任意一条就会回应，再由冷却决定是否真的开口。',
  },
  {
    id: 'qq-permissions',
    label: '权限',
    icon: 'verified_user',
    Page: PermissionsPage,
    note: '谁能对 bot 下管理命令。管理员权限跨所有群和私聊生效，不区分场景。',
  },
  {
    id: 'qq-input',
    label: '输入处理',
    icon: 'attach_file',
    Page: InputPage,
    note: '群友发来的图片、文件和合并转发，哪些收、收多少。',
  },
  {
    id: 'qq-output',
    label: '输出格式',
    icon: 'format_align_left',
    Page: OutputPage,
    note: '消息发出去之前的处理：分条、去掉 Markdown 标记、长度上限、表态。',
  },
  {
    id: 'qq-interaction',
    label: '群互动',
    icon: 'chat',
    Page: InteractionPage,
    note: '除了说话之外还能做什么：跟复读、抢答、给资料卡点赞。',
  },
  {
    id: 'qq-sticker',
    label: '表情包',
    icon: 'mood',
    Page: StickerCollectPage,
    note: '在 QQ 这一侧怎么收表情包、怎么发。图库本身在「机器人 › 表情包图库」里管。',
  },
  {
    id: 'qq-link',
    label: '链路',
    icon: 'lan',
    Page: RuntimePage,
    AlwaysPanel: DiagnosticsPanel,
    note: '队列、群历史、转发桥这几个改完立刻重建运行期对象的参数，以及固定配置与危险操作。',
  },
];

/** 把平台分区包进 QQ 页壳。settingsTabs 注册的是包装后的组件。 */
export function wrapPlatformSection({ label, note, Page, AlwaysPanel, standalone }: PlatformSectionDefinition): () => JSX.Element {
  const Wrapped = () => (
    <QqPage note={note} standalone={standalone} title={label} alwaysContent={AlwaysPanel ? <AlwaysPanel /> : undefined}>
      <Page />
    </QqPage>
  );
  Wrapped.displayName = `QqPage(${label})`;
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
