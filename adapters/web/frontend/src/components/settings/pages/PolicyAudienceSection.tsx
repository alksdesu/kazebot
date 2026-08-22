// 同一条策略规则，对不同来源的人结果不同。这一层判定写在 supervisor/state.py 里，
// 规则表看不出来，出过「界面说没权限、实际写成了」的困惑。
import { Facts, Footnote } from './settingsControls';
import { Card } from './settingsPagePrimitives';

type Outcome = 'run' | 'ask' | 'deny';

const OUTCOME_TEXT: Record<Outcome, string> = {
  run: '直接执行',
  ask: '弹审批',
  deny: '拒绝',
};

const OUTCOME_TONE: Record<Outcome, string> = {
  run: 'text-[var(--duties-live)]',
  ask: 'text-[var(--duties-text)]',
  deny: 'text-[var(--duties-danger)]',
};

interface Audience {
  who: string;
  note: string;
  /** 依次对应：规则判 auto / 需审批但非敏感 / 需审批且敏感 / 规则判 deny */
  outcomes: [Outcome, Outcome, Outcome, Outcome];
}

const AUDIENCES: Audience[] = [
  {
    who: 'QQ 群友',
    note: '不在管理员名单里的人',
    outcomes: ['deny', 'deny', 'deny', 'deny'],
  },
  {
    who: 'QQ 管理员',
    note: '名单里的人',
    outcomes: ['run', 'run', 'ask', 'deny'],
  },
  {
    who: '定时任务',
    note: '没有人在场，审批没人点',
    outcomes: ['run', 'run', 'deny', 'deny'],
  },
  {
    who: '网页端',
    note: '就是你现在用的这个界面',
    outcomes: ['run', 'ask', 'ask', 'deny'],
  },
];

const COLUMNS = ['规则判 auto', '需审批·普通', '需审批·敏感', '规则判 deny'];

const CELL = 'border-b border-[var(--duties-border)] px-3 py-2 text-left';

export const PolicyAudienceSection = () => (
  <Card
    description="上面那张规则表只是一半。同一条规则，命中它的人不同，结果不同 —— 这一层判定在服务端，规则表里看不出来。"
    scope="all-channels"
    title="谁在触发"
  >
    <div className="overflow-x-auto border border-[var(--duties-border)] bg-[var(--duties-panel)]">
      <table className="w-full border-collapse font-mono text-xs">
        <thead>
          <tr>
            <th className={`${CELL} font-semibold`}>触发者</th>
            {COLUMNS.map((label) => (
              <th className={`${CELL} font-semibold`} key={label}>{label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {AUDIENCES.map((row) => (
            <tr key={row.who}>
              <th className={`${CELL} font-medium last:border-b-0`} scope="row">
                {row.who}
                <span className="mt-0.5 block text-[0.6rem] font-normal text-[var(--duties-tertiary)]">
                  {row.note}
                </span>
              </th>
              {row.outcomes.map((outcome, index) => (
                <td className={`${CELL} ${OUTCOME_TONE[outcome]}`} key={COLUMNS[index]}>
                  {OUTCOME_TEXT[outcome]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>

    <Facts>
      「敏感」指这次操作命中了上面规则表里某一条明确写出来的路径；只落到 default 的不算。
      所以同样是写文件，管理员写 <code className="text-[0.65rem]">data/attachments/</code> 不会问你，
      写 <code className="text-[0.65rem]">engine/</code> 会。
    </Facts>

    <Footnote>
      QQ 群友那一行只有一个例外：纯读公网的 curl GET 会直接放行，因为他们没有点审批的入口，
      而读网页没有副作用。这一层只管 read_file、write_file、execute_command、restart 四件事；
      别的工具（联网搜索、存记忆、发表情包等）不经过这一页，能不能调只看节点授权。
    </Footnote>
  </Card>
);
