// 表情包在 QQ 这一侧的收集与发送。图库本身是全平台共用的，在「机器人 › 表情包图库」里管。
import { QQ_NAPCAT } from '../platforms/registry';
import { SchemaFields } from './FieldRenderer';
import type { FieldSpec } from './fieldSchema';

const SECTION = 'sticker';

const STRATEGY_CHOICES: ReadonlyArray<readonly [string, string]> = [
  ['strict', '严格'],
  ['loose', '宽松'],
  ['none', '全收'],
];

const LOOKUP_CHOICES: ReadonlyArray<readonly [string, string]> = [
  ['native_first', '收藏表情'],
  ['library_first', '图库'],
];

const SCHEMA: FieldSpec[] = [
  {
    kind: 'block',
    title: '收集',
    hint: '群里刷过的表情包攒进图库的规则',
    children: [{
      kind: 'grid',
      children: [
        {
          kind: 'bool',
          key: 'sticker_collect',
          label: '收集表情包',
          desc: '把群里刷过的表情包攒进图库。',
          children: [
            { kind: 'desc', text: '严格只收 QQ 标了表情包的，宽松再放行看着不像截图的，全收连群友随手发的照片都要。' },
            { kind: 'choice', key: 'sticker_strategy', label: '收多严', choices: STRATEGY_CHOICES, fallback: 'loose' },
            { kind: 'sub', key: 'sticker_auto_accept', label: '收到就直接入库，跳过人工筛选' },
            {
              kind: 'ids',
              key: 'sticker_groups',
              label: '只从这些群收',
              empty: '所有已授权的群都收',
              placeholder: '输入群号，回车添加',
            },
          ],
        },
        {
          kind: 'fixed',
          label: '容量',
          children: [
            { kind: 'desc', text: '待审池满了就不再收新的；图库超出上限时最旧的先出局。' },
            { kind: 'number', key: 'sticker_pending_limit', label: '待审池', step: 10, unit: '张' },
            { kind: 'number', key: 'sticker_library_limit', label: '图库', step: 50, unit: '张' },
            { kind: 'number', key: 'sticker_pending_ttl_days', label: '待审保留', unit: '天' },
          ],
        },
        {
          kind: 'bool',
          key: 'sticker_auto_tag',
          label: '自动打标签',
          desc: '收进来的图自动送去看图渠道打标签。关掉就不再产生这笔开销，但没标签的图检索不到，也就发不出去。',
          fallback: true,
        },
      ],
    }],
  },
  {
    kind: 'block',
    title: '发送',
    hint: '什么时候把表情包摊给模型挑',
    children: [{
      kind: 'grid',
      children: [
        {
          kind: 'fixed',
          label: '发图',
          children: [
            { kind: 'desc', text: '清单按概率整份摊给模型，发不发、发哪张由它自己定。库比上限大就先给发得最少的。' },
            { kind: 'number', key: 'sticker_send_probability', label: '每轮出现概率', max: 1, step: 0.05 },
            { kind: 'number', key: 'sticker_prompt_limit', label: '最多列出', unit: '张' },
            { kind: 'number', key: 'sticker_repeat_window_sec', label: '同图间隔', step: 60, unit: '秒' },
            { kind: 'desc', text: '模型写的表情名同时对得上收藏表情和图库里的图时，先发哪一边的。' },
            { kind: 'choice', key: 'sticker_lookup_order', label: '重名时先用', choices: LOOKUP_CHOICES, fallback: 'native_first' },
          ],
        },
        {
          kind: 'bool',
          key: 'sticker_combat',
          label: '接梗',
          desc: '群里连着刷图时跟着接一张。要求连图来自至少两个人，中间夹了纯文本就不算。',
          children: [
            { kind: 'number', key: 'sticker_burst_probability', label: '发完再补一张的概率', max: 1, step: 0.05 },
          ],
        },
      ],
    }],
  },
  {
    kind: 'note',
    text: '只有在库且打过标的图才会进候选 —— 没打标的图对模型来说是不可描述的，给了也挑不出来。'
      + '打标走「机器人 › 表情包图库」里显示的那条看图渠道，跟着 bot 进程慢慢跑，刚收进来的图要等一会儿才可用。',
  },
];

export const StickerCollectPage = () => (
  <SchemaFields platformId={QQ_NAPCAT.id} sectionId={SECTION} spec={SCHEMA} />
);
