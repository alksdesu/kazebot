// QQ · NapCat 页上前端自己画的部分：各分区收起时的摘要，和布局里 custom 节点点名的组件。
// 分区本身、字段和说明都来自后端的 schema，这里只放编不成字段的东西。
import { Audition } from './Audition';
import { Capabilities } from './CapabilityGrants';
import { BridgeFacts, Environment, HistoryFacts, QueueFacts, RestartEngine } from './LinkFacts';
import { AdminListWarning, GroupListWarning } from './listWarnings';
import type { PageParts } from './sectionParts';
import {
  ChannelsSummary,
  InputSummary,
  InteractionSummary,
  LinkSummary,
  OutputSummary,
  PermissionsSummary,
  StickerSummary,
  TriggerSummary,
} from './sectionSummaries';

export const QQ_PARTS: PageParts = Object.freeze({
  channels: { Summary: ChannelsSummary, components: { groupWarning: GroupListWarning } },
  trigger: { Summary: TriggerSummary, components: { audition: Audition } },
  permissions: {
    Summary: PermissionsSummary,
    components: { adminWarning: AdminListWarning, capabilities: Capabilities },
  },
  input: { Summary: InputSummary },
  output: { Summary: OutputSummary },
  interaction: { Summary: InteractionSummary },
  sticker: { Summary: StickerSummary },
  link: {
    Summary: LinkSummary,
    components: {
      queueFacts: QueueFacts,
      historyFacts: HistoryFacts,
      bridgeFacts: BridgeFacts,
      environment: Environment,
      restartEngine: RestartEngine,
    },
  },
});
