// 权限：谁是管理员，以及每项能力放到哪一档。能力清单由 bot 公布，这一页只负责显示和写回。
import type { QqCapability } from '../api/supervisorClient';
import { Block, Footnote, Grid } from './components';
import { useCapabilities, useConsoleStore, useIdList, useLiveValue, useNote } from './consoleStore';
import { BoolOption } from './fields';
import { IdList } from './IdList';

// 档位是固定的四个枚举，不像能力那样会增长，所以说法留在前端。
// 「加」表示在前一档基础上累加 —— 选了群主，名单里的人一样还能用。
const GRANT_LABELS: Record<string, string> = {
  off: '不开放',
  admin: '仅管理员',
  owner: '加群主',
  group_admin: '加群管',
};

const GROUP_ROLE_GRANTS = new Set(['owner', 'group_admin']);

const EmptyWarning = () => {
  const admins = useIdList('admin_users');
  const note = useNote('admin_users');
  if (admins.length) return null;
  return <p className="qc-login-error">{note || '名单为空，管理命令全部不可用。'}</p>;
};

const CapabilityRow = ({ cap }: { cap: QqCapability }) => {
  const setDraft = useConsoleStore((state) => state.setDraft);
  const configKey = `grant_${cap.key}`;
  const grant = useLiveValue<string>(configKey, cap.default);
  // 「本群」只在群角色真拿到这项能力时才有话说，那时它是在讲这项授权的边界。
  const scoped = cap.group_scoped && GROUP_ROLE_GRANTS.has(grant);

  return (
    <li className="qc-cap">
      <div className="qc-cap-head">
        <span className="qc-cap-name">{cap.name}</span>
        {scoped && <span className="qc-cap-scope">只在本群</span>}
        <span className="qc-bar-spacer" />
        <div className="qc-grants" role="group">
          {cap.grants.map((option) => (
            <button
              aria-pressed={option === grant}
              className={`qc-grant${option === grant ? ' qc-grant-on' : ''}`}
              key={option}
              onClick={() => setDraft(configKey, option)}
              type="button"
            >
              {GRANT_LABELS[option] || option}
            </button>
          ))}
        </div>
      </div>
      <p className="qc-cap-desc">{cap.desc}</p>
    </li>
  );
};

const Capabilities = () => {
  const caps = useCapabilities();
  if (!caps.length) {
    return <p className="qc-facts">bot 还没公布能力清单，连上之后这里才会有内容。</p>;
  }
  return (
    <ul className="qc-caps">
      {caps.map((cap) => (
        <CapabilityRow cap={cap} key={cap.key} />
      ))}
    </ul>
  );
};

export const PermissionsPage = () => (
  <>
    <Block hint="名单里的 QQ 号在所有会话里都算管理员" title="管理员">
      <div className="qc-panel">
        <IdList
          configKey="admin_users"
          empty="名单为空"
          label="管理员 QQ"
          placeholder="输入 QQ 号，回车添加"
        />
        <EmptyWarning />
      </div>
    </Block>

    <Block hint="每项各自决定放到哪一档" title="这些人能做什么">
      <Capabilities />
    </Block>

    <Block hint="审批卡片怎么认回来" title="审批">
      <Grid>
        <BoolOption
          configKey="approval_reply_unique_fallback"
          desc="引用了审批卡片却取不到编号时，按「当前唯一待审批」处理。"
          label="唯一待审批兜底"
        >
          <p className="qc-opt-desc">
            默认关：这条兜底分不清引用的是它、还是另一张已经处理过的卡片，是误批的直接成因。开了也仍然要求只剩一条待审批、且那张卡片确实发给了本人。
          </p>
        </BoolOption>
        <BoolOption
          configKey="approval_bare_verb_unique"
          desc="不引用卡片、整句只发「同意」时，按当前唯一待审批处理。"
          label="直接回同意"
        >
          <p className="qc-opt-desc">
            默认开：收到卡片后直接回「同意」就能批。只在你名下恰好剩一条待审批、且那张卡片确实发给你时成立；有多条会让你指定，一条都没有时就当普通聊天。
          </p>
        </BoolOption>
      </Grid>
    </Block>

    <Footnote>
      群主与群管理员的身份由 QQ 上报，只在群消息里成立 —— 同一个人在私聊里就是普通用户。
      影响面超出单个群的那几项（改全局配置、借 bot 的身份发给任意人）只能给名单里的人，
      它们的档位里没有群主可选；标了「只在本群」的项给出去之后，目标会被夹在他自己那个群里。
      名单还兼着私聊放通和审批收件人，把能力调成「不开放」不会把人从名单里去掉。
    </Footnote>
  </>
);
