// 开口时机。七个信号里的三态那几项留空表示跟随旧的 group_mode 推导，界面上要说清。
import { QQ_NAPCAT } from '../platforms/registry';
import { Audition } from './Audition';
import { SchemaFields } from './FieldRenderer';
import { registerSectionComponents, type FieldSpec } from './fieldSchema';

const SECTION = 'trigger';

const CODE = 'bg-[var(--duties-muted)] px-1 py-px font-mono text-mini';

const PREFIX_MODES = ['prefix', 'prefix_or_mention', 'mention_or_prefix'];

const legacyMode = (value: unknown): string => String(value ?? 'mention_only').toLowerCase();

const PrefixFootnote = () => (
  <>
    前缀同时用于剥离正文，但 <code className={CODE}>/</code> 与 <code className={CODE}>／</code> 例外 ——
    <code className={CODE}>/draw</code> 这类命令要求字面斜杠，剥掉会失配。
  </>
);

registerSectionComponents(QQ_NAPCAT.id, SECTION, { audition: Audition });

const SCHEMA: FieldSpec[] = [
  {
    kind: 'block',
    title: '触发条件',
    hint: '命中任意一条即回应',
    children: [
      {
        kind: 'grid',
        children: [
          {
            kind: 'tristate',
            key: 'signal_all',
            label: '全量回应',
            desc: '群里任何人说任何话都回。开着的时候下面几条都不再影响判定。',
            fallback: { from: 'group_trigger', when: (value) => ['all', 'always'].includes(legacyMode(value)) },
          },
          {
            kind: 'tristate',
            key: 'signal_at',
            label: '被 @',
            desc: '消息里任何位置 @ 到 bot，不限句首。',
            fallback: true,
          },
          {
            kind: 'tristate',
            key: 'signal_reply',
            label: '被回复',
            desc: '有人引用 bot 说过的话。',
            fallback: true,
          },
          {
            kind: 'bool',
            key: 'signal_name',
            label: '出现名字',
            desc: '消息里提到 bot 的名字。原文会完整交给模型，不会被剥掉。',
            children: [
              {
                kind: 'words',
                key: 'name_words',
                label: '认这些名字',
                empty: '还没有名字，这一条不会命中',
                placeholder: '输入一个名字，回车添加',
              },
              { kind: 'sub', key: 'name_anywhere', label: '不限句首，句中提到也算', fallback: true },
            ],
          },
          {
            kind: 'tristate',
            key: 'signal_prefix',
            label: '前缀触发',
            desc: '以配置的前缀开头。',
            fallback: { from: 'group_trigger', when: (value) => PREFIX_MODES.includes(legacyMode(value)) },
          },
          {
            kind: 'bool',
            key: 'signal_keyword',
            label: '关键词插话',
            desc: '聊到特定话题时主动接话。',
            children: [{
              kind: 'words',
              key: 'keyword_words',
              label: '听这些词',
              empty: '还没有关键词，这一条不会命中',
              placeholder: '输入一个关键词，回车添加',
            }],
          },
          {
            kind: 'bool',
            key: 'signal_random',
            label: '随机插话',
            desc: '按概率随便说两句。骰子在运行时才掷，试听里只标注为「看运行时」。',
            children: [{ kind: 'percent', key: 'random_probability', label: '每条消息' }],
          },
        ],
      },
      {
        kind: 'loose',
        children: [
          {
            kind: 'words',
            key: 'trigger_prefixes',
            label: '触发前缀',
            empty: '没有前缀，前缀触发不会命中',
            placeholder: '输入一个前缀，回车添加',
            spacing: 'loose',
          },
          { kind: 'note', text: <PrefixFootnote /> },
        ],
      },
    ],
  },
  {
    kind: 'block',
    title: '接话意愿判断',
    hint: '前面全不命中时才问模型',
    children: [{
      kind: 'grid',
      children: [
        {
          kind: 'bool',
          key: 'llm_intent_enabled',
          label: '让模型判断',
          desc: '问一次轻量模型「这句话需要你接话吗」。每条不命中的群消息都会产生一次模型往返，务必配独立的便宜渠道。',
          children: [
            { kind: 'text', key: 'llm_intent_node_id', label: '判定节点' },
            { kind: 'number', key: 'llm_intent_context_messages', label: '带上最近', unit: '条' },
          ],
        },
        {
          kind: 'fixed',
          label: '并发上限',
          children: [
            { kind: 'desc', text: '判定会占住 engine worker，占满了真实对话就得排队。' },
            { kind: 'number', key: 'llm_intent_max_inflight', label: '同时最多', unit: '个' },
            { kind: 'number', key: 'llm_intent_timeout_sec', label: '超时', step: 0.5, unit: '秒' },
          ],
        },
      ],
    }],
  },
  {
    kind: 'block',
    title: '冷却',
    hint: '命中之后仍可能不说话',
    children: [{
      kind: 'grid',
      children: [
        {
          kind: 'fixed',
          label: '发言间隔',
          children: [
            { kind: 'desc', text: '刚说过话的群里先歇一会儿。0 表示不限制。' },
            { kind: 'number', key: 'cooldown_group_sec', label: '每群', unit: '秒' },
            { kind: 'number', key: 'cooldown_user_sec', label: '每人', unit: '秒' },
          ],
        },
        {
          kind: 'bool',
          key: 'cooldown_exempt_at',
          label: '被 @ 时无视冷却',
          desc: '有人直接叫 bot，就不该被静默忽略。',
          fallback: true,
        },
      ],
    }],
  },
  {
    kind: 'block',
    title: '试听',
    hint: '判定由 bot 那份代码算，不是界面自己猜的',
    children: [{ kind: 'custom', name: 'audition' }],
  },
  {
    kind: 'note',
    text: '真实判定由 bot 进程执行。改动要点「应用」写入配置文件之后才生效，顶部状态条会显示 bot'
      + ' 是否已经读到这一份。试听用的是编辑中的配置，所以它显示的是「应用之后会怎样」。',
  },
];

export const TimingPage = () => (
  <SchemaFields platformId={QQ_NAPCAT.id} sectionId={SECTION} spec={SCHEMA} />
);
