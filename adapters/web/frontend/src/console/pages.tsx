// QQ 域各页在设置侧栏里的登记项。页面本体只写内容，外壳与生效状态条由 QqPage 统一给。
import { AccountPage } from './AccountPage';
import { ChannelsPage } from './ChannelsPage';
import { QqPage } from './components';
import { MemoryPage } from './MemoryPage';
import { ModelsPage } from './ModelsPage';
import { PermissionsPage } from './PermissionsPage';
import { PersonaPage } from './PersonaPage';
import { ProvidersPage } from './ProvidersPage';
import { RuntimePage } from './RuntimePage';
import { StickersPage } from './StickersPage';
import { TimingPage } from './TimingPage';

export interface QqPageDefinition {
  /** 设置页 tab id。带 qq- 前缀：console 的「运行」和设置页的「运行」诊断页同名，不加前缀会撞。 */
  id: string;
  label: string;
  icon: string;
  note: string;
  Page: () => JSX.Element;
  /** 写的不是 qq.yaml。bot 没跑也该能改，不等它公布生效配置。 */
  standalone?: boolean;
}

export const QQ_PAGES: QqPageDefinition[] = [
  {
    id: 'qq-account',
    label: '账号',
    icon: 'account_circle',
    Page: AccountPage,
    standalone: true,
    note: '两件事：换这一个实例登录的号（会重启 NapCat，期间 bot 不可用，新号不在原来的群里就得重配名单），和让多个号同时在线（多开，每个号一套独立进程与数据）。',
  },
  {
    id: 'qq-channels',
    label: '信道',
    icon: 'inbox',
    Page: ChannelsPage,
    note: 'bot 只在名单里的群和私聊里出现。两份名单都是白名单，不在名单里的消息连处理都不会处理。',
  },
  {
    id: 'qq-timing',
    label: '时机',
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
    id: 'qq-persona',
    label: '人格',
    icon: 'draft',
    Page: PersonaPage,
    note: 'bot 是谁、能做什么。上半页写的是节点文件，各自存盘；下半页走 qq.yaml，改完点顶部的应用。',
  },
  {
    id: 'qq-memory',
    label: '记忆',
    icon: 'psychology',
    Page: MemoryPage,
    standalone: true,
    note: 'bot 记住了什么、每个会话攒了多少上下文。改动直接落盘，不用点应用。',
  },
  {
    id: 'qq-stickers',
    label: '表情包',
    icon: 'photo_library',
    Page: StickersPage,
    note: 'bot 能发哪些表情包。群里收来的先进待审，人工转正之后才进候选。看图渠道决定图能不能被打上标签，这一块自己存盘；底部的收集设置走 qq.yaml，改完点应用。',
  },
  {
    id: 'qq-link',
    label: '链路',
    icon: 'lan',
    Page: RuntimePage,
    note: '进程与链路的实况，以及几个改完立刻重建运行期对象的参数。',
  },
  {
    id: 'qq-providers',
    label: '渠道',
    icon: 'hub',
    Page: ProvidersPage,
    standalone: true,
    note: 'bot 用哪一家的模型，以及它的地址和密钥。改完下一条消息就生效，不用重启。',
  },
  {
    id: 'qq-models',
    label: '模型',
    icon: 'model_training',
    Page: ModelsPage,
    standalone: true,
    note: '发给模型的请求里带哪些参数。清单由各家 provider 自己声明，全局与节点分两层，节点优先。',
  },
];

/** 把域页包进共同外壳。settingsTabs 注册的是包装后的组件。 */
export function wrapQqPage({ label, note, Page, standalone }: QqPageDefinition): () => JSX.Element {
  const Wrapped = () => (
    <QqPage note={note} standalone={standalone} title={label}>
      <Page />
    </QqPage>
  );
  Wrapped.displayName = `QqPage(${label})`;
  return Wrapped;
}
