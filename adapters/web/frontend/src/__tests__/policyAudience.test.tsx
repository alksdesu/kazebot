// 这张表画的是 supervisor/state.py 的 request_operation 判定。
// 它和规则表分开看会得出相反结论——出过一次：界面显示「有权限」而群友实际被硬拒，
// 也出过反过来的困惑：规则写着 approval_required，管理员触发却直接执行了。
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PolicyAudienceSection } from '../components/settings/pages/PolicyAudienceSection';

const rowFor = (who: string) =>
  screen.getByRole('rowheader', { name: new RegExp(who) }).closest('tr')!;

describe('谁在触发', () => {
  it('四种来源都摆出来', () => {
    render(<PolicyAudienceSection />);

    for (const who of ['QQ 群友', 'QQ 管理员', '定时任务', '网页端']) {
      expect(screen.getByRole('rowheader', { name: new RegExp(who) })).toBeInTheDocument();
    }
  });

  it('群友那一行四格全是拒绝', () => {
    // state.py 对 QQ 非管理员的 read_file/write_file/execute_command/restart 是前置硬 deny，
    // 规则表说 auto 也没用。这一行只要有一格不是拒绝就是画错了。
    render(<PolicyAudienceSection />);

    const cells = within(rowFor('QQ 群友')).getAllByRole('cell');
    expect(cells).toHaveLength(4);
    for (const cell of cells) expect(cell).toHaveTextContent('拒绝');
  });

  it('管理员碰到「需审批·普通」是直接执行，不是弹审批', () => {
    // 这一格就是「界面说要审批、实际没问就写了」的出处。
    render(<PolicyAudienceSection />);

    const cells = within(rowFor('QQ 管理员')).getAllByRole('cell');
    expect(cells[1]).toHaveTextContent('直接执行');
    expect(cells[2]).toHaveTextContent('弹审批');
  });

  it('定时任务碰到敏感规则是拒绝——没人在场点审批', () => {
    render(<PolicyAudienceSection />);

    const cells = within(rowFor('定时任务')).getAllByRole('cell');
    expect(cells[2]).toHaveTextContent('拒绝');
  });

  it('规则判 deny 时谁都拒绝', () => {
    render(<PolicyAudienceSection />);

    for (const who of ['QQ 群友', 'QQ 管理员', '定时任务', '网页端']) {
      expect(within(rowFor(who)).getAllByRole('cell')[3]).toHaveTextContent('拒绝');
    }
  });

  it('说明里点出这一层只管四个操作', () => {
    // 别的工具不经过 policy，用户按这张表推断「所有工具都受管」会高估防护。
    render(<PolicyAudienceSection />);

    expect(screen.getByText(/read_file、write_file、execute_command、restart/)).toBeInTheDocument();
  });
});
