// 信道：bot 在哪些群说话、谁能私聊。两份名单方向相反 —— 群空名单等于关门，
// 私聊空名单只是不额外放行，仍走下面的好友规则。
import { Block, Footnote, Grid } from './components';
import { useIdList, useNote } from './consoleStore';
import { BoolOption, TextField } from './fields';
import { IdList } from './IdList';

const GroupWarning = () => {
  const groups = useIdList('allowed_groups');
  const note = useNote('allowed_groups');
  if (groups.length) return null;
  return <p className="qc-login-error">{note || '名单为空，bot 不会在任何群说话。'}</p>;
};

export const ChannelsPage = () => (
  <>
    <Block hint="不在名单里的群，消息一律不处理" title="群">
      <div className="qc-panel">
        <IdList
          configKey="allowed_groups"
          empty="名单为空"
          label="允许说话的群"
          placeholder="输入群号，回车添加"
        />
        <GroupWarning />
      </div>
    </Block>

    <Block hint="私聊按名单与好友关系两条独立放行" title="私聊">
      <div className="qc-panel">
        <IdList
          configKey="allowed_private_users"
          empty="名单为空，只按下面的好友规则放行"
          label="允许私聊的 QQ"
          placeholder="输入 QQ 号，回车添加"
        />
      </div>
      <Grid>
        <BoolOption
          configKey="allow_private_friends"
          desc="已经通过好友请求的人不必再进名单。关掉之后只有上面名单里的人能私聊。"
          fallback
          label="好友自动放行"
        />
        <TextField configKey="private_denied_reply" label="拒绝时回复" />
      </Grid>
    </Block>

    <Footnote>
      备注只存在这台浏览器里，不写进 qq.yaml、也不会进模型的上下文 —— 群名往往带真实身份。
      换个浏览器或清掉站点数据，备注就没了，名单本身不受影响。
    </Footnote>
  </>
);
