// 信道：bot 在哪些群说话、谁能私聊。两份名单方向相反 —— 群空名单等于关门，
// 私聊空名单只是不额外放行，仍走下面的好友规则。
import { QQ_NAPCAT } from '../platforms/registry';
import { ErrorText } from './components';
import { useIdList, useNote } from './consoleStore';
import { SchemaFields } from './FieldRenderer';
import { registerSectionComponents, type FieldSpec } from './fieldSchema';

const SECTION = 'channels';

const GroupWarning = () => {
  const groups = useIdList('allowed_groups');
  const note = useNote('allowed_groups');
  if (groups.length) return null;
  return <ErrorText>{note || '名单为空，bot 不会在任何群说话。'}</ErrorText>;
};

registerSectionComponents(QQ_NAPCAT.id, SECTION, { groupWarning: GroupWarning });

const SCHEMA: FieldSpec[] = [
  {
    kind: 'block',
    title: '群',
    hint: '不在名单里的群，消息一律不处理',
    children: [{
      kind: 'panel',
      children: [
        {
          kind: 'ids',
          key: 'allowed_groups',
          label: '允许说话的群',
          empty: '名单为空',
          placeholder: '输入群号，回车添加',
          spacing: 'panel',
        },
        { kind: 'custom', name: 'groupWarning' },
      ],
    }],
  },
  {
    kind: 'block',
    title: '私聊',
    hint: '私聊按名单与好友关系两条独立放行',
    children: [
      {
        kind: 'panel',
        children: [{
          kind: 'ids',
          key: 'allowed_private_users',
          label: '允许私聊的 QQ',
          empty: '名单为空，只按下面的好友规则放行',
          placeholder: '输入 QQ 号，回车添加',
          spacing: 'panel',
        }],
      },
      {
        kind: 'grid',
        children: [
          {
            kind: 'bool',
            key: 'allow_private_friends',
            label: '好友自动放行',
            desc: '已经通过好友请求的人不必再进名单。关掉之后只有上面名单里的人能私聊。',
            fallback: true,
          },
          { kind: 'text', key: 'private_denied_reply', label: '拒绝时回复' },
        ],
      },
    ],
  },
  {
    kind: 'note',
    text: '备注只存在这台浏览器里，不写进平台配置、也不会进模型的上下文 —— 群名往往带真实身份。'
      + '换个浏览器或清掉站点数据，备注就没了，名单本身不受影响。',
  },
];

export const ChannelsPage = () => (
  <SchemaFields platformId={QQ_NAPCAT.id} sectionId={SECTION} spec={SCHEMA} />
);
