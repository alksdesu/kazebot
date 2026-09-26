// 输出格式：消息发出去之前的处理。
import { QQ_NAPCAT } from '../platforms/registry';
import { SchemaFields } from './FieldRenderer';
import type { FieldSpec } from './fieldSchema';

const SECTION = 'output';

const SCHEMA: FieldSpec[] = [
  {
    kind: 'block',
    title: '回复格式',
    hint: '消息发出去之前的处理',
    children: [{
      kind: 'grid',
      children: [
        {
          kind: 'bool',
          key: 'reply_to_trigger',
          label: '引用原消息',
          desc: '回复时引用触发它的那条消息，群里热闹时能看清在回谁。',
          fallback: true,
        },
        {
          kind: 'bool',
          key: 'strip_asterisk_styles',
          label: '去掉星号强调',
          desc: 'QQ 不渲染 Markdown，*强调* 会原样显示成星号。',
          fallback: true,
        },
        {
          kind: 'bool',
          key: 'strip_underscore_styles',
          label: '去掉下划线强调',
          desc: '下划线同理。但代码和文件名里常有下划线，默认不动。',
        },
        {
          kind: 'fixed',
          label: '长度上限',
          children: [
            { kind: 'desc', text: '超长的回复会被截断，避免被 QQ 拒收。' },
            { kind: 'number', key: 'message_limit', label: '单条消息', step: 100, unit: '字' },
            { kind: 'number', key: 'history_text_limit', label: '历史里每条', step: 50, unit: '字' },
          ],
        },
        {
          kind: 'bool',
          key: 'enable_image_forward_merge',
          label: '合并发图',
          desc: '多张图合并成一条转发发出，而不是一张张发。',
          children: [
            { kind: 'number', key: 'image_forward_merge_threshold', label: '至少', unit: '张才合并' },
            {
              kind: 'desc',
              text: '默认关是有原因的：合并转发会长时间占住那条唯一的反向 WS，期间发消息、贴表情全部排队。发现卡顿就关掉，即时生效。',
            },
          ],
        },
      ],
    }],
  },
  {
    kind: 'block',
    title: '表态',
    hint: '告诉模型有哪些收藏表情可用',
    children: [{
      kind: 'grid',
      children: [
        { kind: 'bool', key: 'enable_reactions', label: '表情回应', desc: '给群消息贴表情回应。', fallback: true },
        {
          kind: 'fixed',
          label: '表情提示',
          children: [
            { kind: 'desc', text: '告诉模型有哪些收藏表情可用。0 表示不告诉它。' },
            { kind: 'number', key: 'face_prompt_limit', label: '最多列出', unit: '个' },
          ],
        },
      ],
    }],
  },
  {
    kind: 'note',
    text: '这些是 QQ 这一侧的发送处理，改完点顶部的「应用」。bot 是谁、说话什么风格在「机器人 › 人格」里改。',
  },
];

export const OutputPage = () => (
  <SchemaFields platformId={QQ_NAPCAT.id} sectionId={SECTION} spec={SCHEMA} />
);
