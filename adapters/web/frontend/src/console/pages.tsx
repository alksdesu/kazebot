// 控制台各页在设置侧栏里的登记项。页面本体只写内容，外壳由这里统一套：
// 平台页按平台清单生成，走 PlatformPage（状态栏 + 连接卡 + 折叠分区，分区来自后端 schema），机器人页与模型页走通用素壳。
import { Empty } from '../components/settings/pages/settingsControls';
import { DiagnosticsPanel } from '../features/operations/DiagnosticsPanel';
import { PlainPage } from './components';
import { AccountSettingsPage } from './AccountSettingsPage';
import { FEATURES_SCOPE, type ConfigScope } from './configScope';
import { ConnectionCard } from './ConnectionCard';
import { DEFAULTS_PAGE_SECTIONS, STICKERS_PAGE_SECTIONS } from './FeatureSection';
import { FeaturesPage } from './FeaturesPage';
import { nameBetween } from './format';
import { MemoryPage } from './MemoryPage';
import { ModelsPage } from './ModelsPage';
import { PersonaPage } from './PersonaPage';
import { PlatformPage } from './PlatformPage';
import { ProvidersPage } from './ProvidersPage';
import { QQ_PARTS } from './qqParts';
import { NO_PARTS, type PageParts } from './sectionParts';
import type { SectionPick } from './sectionPick';
import type { SearchableSection } from './schemaSearch';
import { StickersPage } from './StickersPage';
import { QQ_NAPCAT, type PlatformDescriptor } from '../platforms/registry';
import { usePlatformRegistryState } from '../platforms/usePlatformRegistry';

export interface ConsolePageDefinition {
  /** 设置页 tab id。改名要在 settingsTabAliases 里留一条别名，老书签才不会失效。 */
  id: string;
  label: string;
  icon: string;
  note: string;
  Page: () => JSX.Element;
  /** 这页摆的配置区块来自哪份控制台 schema。侧栏搜索按它索引分区名和字段标签。 */
  configScope?: ConfigScope;
  /** 这页摆那份 schema 里的哪几块。不给就是整份。 */
  sections?: SectionPick;
  searchSections?: readonly SearchableSection[];
}

/** 不分平台的那几页：它是谁、记得什么、图库里有什么。 */
export const BOT_PAGES: ConsolePageDefinition[] = [
  {
    id: 'account',
    label: '账号与连接',
    icon: 'account_circle',
    Page: AccountSettingsPage,
    note: '管理实例、共享配置和当前实例的账号连接。每个实例只连接一个平台、登录一个账号。',
    searchSections: [{
      id: 'account', label: '账号与连接',
      text: '账号与连接 account 多开实例 共享配置 机器人账号 快速切换 记忆归属',
    }],
  },
  {
    id: 'persona',
    label: '人格',
    icon: 'draft',
    Page: PersonaPage,
    note: 'bot 是谁、用哪个模型。两块各自保存，写的是这个实例的节点文件。',
  },
  {
    id: 'defaults',
    label: '全局默认',
    icon: 'public',
    Page: FeaturesPage,
    configScope: FEATURES_SCOPE,
    sections: DEFAULTS_PAGE_SECTIONS,
    note: '平台页没单独设置时用的值。每块各自保存，写进全局设置，不用重启；平台页里选了「本平台」的项不跟着变。',
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
    configScope: FEATURES_SCOPE,
    sections: STICKERS_PAGE_SECTIONS,
    note: '这台机器上的实例共用一份图库。群里收来的先进待审，人工转正之后才进候选；每块自己保存。',
  },
];

/** 模型与渠道。写的是 config/models.yaml，和任何平台都无关。 */
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

export interface PlatformPageDefinition {
  id: string;
  label: string;
  icon: string;
  note: string;
  platformId: string;
}

const PLATFORM_PAGE_PREFIX = 'platform-';

export const DEFAULT_PLATFORM_ICON = 'inbox';

export const platformPageId = (platformId: string): string => `${PLATFORM_PAGE_PREFIX}${platformId}`;

export const isPlatformPageId = (tabId: string): boolean => tabId.startsWith(PLATFORM_PAGE_PREFIX);

/** 前端给个别平台补的东西。没登记的平台照样有页，分区和字段全按后端 schema 画。 */
interface PlatformPageExtras {
  icon?: string;
  /** 分区里前端自己画的部分：摘要和编不成字段的块。 */
  parts?: PageParts;
}

const PLATFORM_PAGE_EXTRAS: Readonly<Record<string, PlatformPageExtras>> = {
  [QQ_NAPCAT.id]: { parts: QQ_PARTS },
};

const platformNote = (platform: PlatformDescriptor): string =>
  `bot ${nameBetween('在', platform.shortName, '上怎么收发消息')}。改完点顶部的「应用」写进配置文件，状态条会显示 bot 有没有读到这一份。`;

/** 平台组：有配置文件的平台一个一页，跟着平台清单走。 */
export function platformPages(platforms: readonly PlatformDescriptor[]): PlatformPageDefinition[] {
  return platforms
    .filter((platform) => platform.hasConfig)
    .map((platform) => ({
      id: platformPageId(platform.id),
      label: platform.displayName,
      icon: PLATFORM_PAGE_EXTRAS[platform.id]?.icon || DEFAULT_PLATFORM_ICON,
      note: platformNote(platform),
      platformId: platform.id,
    }));
}

const PlatformPageById = ({ platformId }: { platformId: string }) => {
  const platform = usePlatformRegistryState().platforms.find((row) => row.id === platformId);
  if (!platform) {
    return (
      <PlainPage eyebrow="平台" note="后端公布的平台清单里没有这个平台。" title={platformId}>
        <Empty>平台清单里已经没有「{platformId}」了，可能是后端换了版本。从侧栏重新进一个平台页。</Empty>
      </PlainPage>
    );
  }
  return (
    <PlatformPage
      connection={<><ConnectionCard /><DiagnosticsPanel /></>}
      note={platformNote(platform)}
      parts={PLATFORM_PAGE_EXTRAS[platform.id]?.parts || NO_PARTS}
      platform={platform}
    />
  );
};

const platformPageComponents = new Map<string, () => JSX.Element>();

/** 一个平台一页，组件按平台 id 缓存：清单换一份时同一个平台还是同一个组件，页面不会重新挂载。 */
export function platformPageComponent(platformId: string): () => JSX.Element {
  const cached = platformPageComponents.get(platformId);
  if (cached) return cached;
  const Wrapped = () => <PlatformPageById platformId={platformId} />;
  Wrapped.displayName = `PlatformPage(${platformId})`;
  platformPageComponents.set(platformId, Wrapped);
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
