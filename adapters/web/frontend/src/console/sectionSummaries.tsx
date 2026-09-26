// 折叠分区收起时那一行。一律读 bot 公布的生效值，不掺草稿 ——
// 摘要回答的是「现在实际在跑什么」，待应用的改动由头上那个计数表达。
import { useAppliedCount, useAppliedValue, useCapabilities, useConsoleStore, useRuntime } from './consoleStore';

const GROUP_ROLE_GRANTS = new Set(['owner', 'group_admin']);

const joinFacts = (parts: (string | false | null | undefined)[]): string =>
  parts.filter(Boolean).join(' · ');

export const AccountSummary = () => {
  const live = useConsoleStore((state) => state.live);
  const runtime = useRuntime();
  const botAlive = Boolean(live?.published && !live.stale);
  if (!botAlive) return <>{live?.published ? '适配进程心跳中断' : '适配进程没在跑'}</>;
  return <>{joinFacts(['适配进程在线', runtime.onebot_connected ? 'NapCat 已连接' : 'NapCat 未连接'])}</>;
};

export const ChannelsSummary = () => {
  const groups = useAppliedCount('allowed_groups');
  const users = useAppliedCount('allowed_private_users');
  const friends = useAppliedValue<boolean>('allow_private_friends', true);
  return <>{joinFacts([
    groups ? `${groups} 个群` : '没有放行的群',
    friends ? (users ? `私聊好友加 ${users} 人` : '私聊仅好友') : (users ? `私聊仅名单 ${users} 人` : '私聊全关'),
  ])}</>;
};

export const TriggerSummary = () => {
  const signals = [
    ['全量回应', useAppliedValue<boolean | null>('signal_all', null)],
    ['@', useAppliedValue<boolean | null>('signal_at', null)],
    ['回复', useAppliedValue<boolean | null>('signal_reply', null)],
    ['名字', useAppliedValue<boolean | null>('signal_name', null)],
    ['前缀', useAppliedValue<boolean | null>('signal_prefix', null)],
    ['关键词', useAppliedValue<boolean | null>('signal_keyword', null)],
    ['随机', useAppliedValue<boolean | null>('signal_random', null)],
  ] as const;
  const cooldown = useAppliedValue<number>('cooldown_group_sec', 0);
  const on = signals.filter(([, value]) => value).map(([label]) => label);
  return <>{joinFacts([
    on.length ? on.join(' / ') : '没有开着的信号',
    cooldown > 0 ? `冷却 ${cooldown} 秒` : '不冷却',
  ])}</>;
};

export const PermissionsSummary = () => {
  const admins = useAppliedCount('admin_users');
  const capabilities = useCapabilities();
  const values = useConsoleStore((state) => state.live?.state?.values);
  const groupRole = capabilities.filter((cap) => GROUP_ROLE_GRANTS.has(String(values?.[`grant_${cap.key}`] ?? cap.default))).length;
  return <>{joinFacts([
    admins ? `${admins} 个管理员` : '没有管理员',
    capabilities.length ? `群管可用 ${groupRole} 项` : '',
  ])}</>;
};

export const InputSummary = () => {
  const image = useAppliedValue<boolean>('enable_image_input', true);
  const images = useAppliedValue<number>('max_images_per_turn', 0);
  const file = useAppliedValue<boolean>('enable_file_input', true);
  const forward = useAppliedValue<boolean>('enable_forward_msg_input', true);
  const depth = useAppliedValue<number>('forward_msg_max_depth', 0);
  return <>{joinFacts([
    image ? `图片 ≤ ${images} 张` : '不看图',
    file ? '收文件' : '不收文件',
    forward ? `合并转发展开 ${depth} 层` : '不读合并转发',
  ])}</>;
};

export const OutputSummary = () => {
  const limit = useAppliedValue<number>('message_limit', 0);
  const asterisk = useAppliedValue<boolean>('strip_asterisk_styles', true);
  const reactions = useAppliedValue<boolean>('enable_reactions', true);
  return <>{joinFacts([
    limit > 0 ? `单条 ≤ ${limit} 字` : '不限长度',
    asterisk ? '去星号' : '留星号',
    reactions ? '表态开' : '表态关',
  ])}</>;
};

export const InteractionSummary = () => {
  const echo = useAppliedValue<boolean>('enable_echo', false);
  const preempt = useAppliedValue<boolean>('enable_preempt', false);
  const like = useAppliedValue<boolean>('enable_auto_like', false);
  return <>{joinFacts([
    `复读${echo ? '开' : '关'}`,
    `抢答${preempt ? '开' : '关'}`,
    `名片赞${like ? '开' : '关'}`,
  ])}</>;
};

const STRATEGY_TEXT: Record<string, string> = { strict: '严格', loose: '宽松', none: '全收' };

export const StickerSummary = () => {
  const collect = useAppliedValue<boolean>('sticker_collect', false);
  const strategy = useAppliedValue<string>('sticker_strategy', 'loose');
  const probability = useAppliedValue<number>('sticker_send_probability', 0);
  return <>{joinFacts([
    collect ? `收集中（${STRATEGY_TEXT[strategy] || strategy}）` : '不收集',
    probability > 0 ? `每轮 ${Math.round(probability * 100)}% 出现` : '不发图',
  ])}</>;
};

export const LinkSummary = () => {
  const queue = useAppliedValue<boolean>('enable_queue', false);
  const workers = useAppliedValue<number>('queue_workers', 0);
  const bridge = useAppliedValue<boolean>('enable_forward_bridge', true);
  return <>{joinFacts([
    queue ? `队列 ${workers} 个 worker` : '不排队',
    `转发桥${bridge ? '开' : '关'}`,
  ])}</>;
};
