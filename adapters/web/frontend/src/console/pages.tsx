// 控制台各页在设置侧栏里的登记项。页面本体只写内容，外壳由这里统一套：
// 平台页走 PlatformPage（状态栏 + 连接卡 + 折叠分区，分区来自后端 schema），机器人页与模型页走通用素壳。
import { DiagnosticsPanel } from '../features/operations/DiagnosticsPanel';
import { PlainPage } from './components';
import { FEATURES_SCOPE, type ConfigScope } from './configScope';
import { ConnectionCard } from './ConnectionCard';
import { DEFAULTS_PAGE_SECTIONS, STICKERS_PAGE_SECTIONS } from './FeatureSection';
import { FeaturesPage } from './FeaturesPage';
import { MemoryPage } from './MemoryPage';
import { ModelsPage } from './ModelsPage';
import { PersonaPage } from './PersonaPage';
import { PlatformPage } from './PlatformPage';
import { ProvidersPage } from './ProvidersPage';
import { QQ_PARTS } from './qqParts';
import type { PageParts } from './sectionParts';
import type { SectionPick } from './sectionPick';
import { StickersPage } from './StickersPage';
import { QQ_NAPCAT, type PlatformDescriptor } from '../platforms/registry';

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
    id: 'defaults',
    label: '全局默认',
    icon: 'public',
    Page: FeaturesPage,
    configScope: FEATURES_SCOPE,
    sections: DEFAULTS_PAGE_SECTIONS,
    note: '各平台没单独设置时用的值。每块各自保存，写进全局设置，不用重启；平台页里选了「本平台」的项不跟着变。',
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

export interface PlatformPageDefinition {
  id: string;
  label: string;
  icon: string;
  note: string;
  platform: PlatformDescriptor;
  /** 分区里前端自己画的部分。分区、字段和说明本身来自后端 schema。 */
  parts: PageParts;
}

export const PLATFORM_PAGES: PlatformPageDefinition[] = [
  {
    id: 'platform-qq',
    label: QQ_NAPCAT.displayName,
    icon: 'inbox',
    platform: QQ_NAPCAT,
    parts: QQ_PARTS,
    note: 'bot 在 QQ 上怎么收发消息。改完点顶部的「应用」写进配置文件，状态条会显示 bot 有没有读到这一份。',
  },
];

/** 一个平台一页。分区在页内折叠，深链靠 ?section= 定位。 */
export function wrapPlatformPage({ note, parts, platform }: PlatformPageDefinition): () => JSX.Element {
  const Wrapped = () => (
    <PlatformPage
      connection={<><ConnectionCard /><DiagnosticsPanel /></>}
      note={note}
      parts={parts}
      platform={platform}
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
