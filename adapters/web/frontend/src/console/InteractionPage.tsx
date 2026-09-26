// 群互动：除了说话之外还能做什么。
import { QQ_NAPCAT } from '../platforms/registry';
import { SchemaFields } from './FieldRenderer';
import type { FieldSpec } from './fieldSchema';

const SECTION = 'interaction';

const SCHEMA: FieldSpec[] = [
  {
    kind: 'block',
    title: '群内互动',
    hint: '除了说话之外还能做什么',
    children: [{
      kind: 'grid',
      children: [
        {
          kind: 'bool',
          key: 'enable_auto_like',
          label: '自动点赞',
          desc: '给群友的资料卡点赞。',
          children: [{ kind: 'number', key: 'auto_like_times', label: '每人每天', unit: '次' }],
        },
        {
          kind: 'bool',
          key: 'enable_preempt',
          label: '抢答',
          desc: '上一条还没答完就来了新消息时，放弃旧的去答新的。',
        },
        {
          kind: 'bool',
          key: 'enable_echo',
          label: '跟复读',
          desc: '连着几个人刷同一句时，也跟一条。',
          children: [
            { kind: 'number', key: 'echo_threshold', label: '连续', unit: '条才跟' },
            { kind: 'number', key: 'echo_max_length', label: '最长', unit: '字' },
            { kind: 'number', key: 'echo_window_sec', label: '时间窗', unit: '秒' },
            { kind: 'number', key: 'echo_cooldown_sec', label: '跟完歇', unit: '秒' },
            {
              kind: 'desc',
              text: '必须是不同的人发的，同一个人连刷不算。命令、@、回复和长文本一律不跟；表情包按图片本身比对，三个人发三张不同的图不会被当成复读。',
            },
          ],
        },
      ],
    }],
  },
  { kind: 'note', text: '这几项只在群里成立，私聊不受影响。改完点顶部的「应用」。' },
];

export const InteractionPage = () => (
  <SchemaFields platformId={QQ_NAPCAT.id} sectionId={SECTION} spec={SCHEMA} />
);
