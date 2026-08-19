// 权限页。这一页显示的是权限边界，显示错了比显示不出来更糟 ——
// 界面上摆一个 bot 不会兑现的档位，运营者会以为权限已经给出去了。
import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import type { QqCapability, QqInputRule } from '../api/supervisorClient';
import { PermissionsPage } from '../console/PermissionsPage';
import { useConsoleStore } from '../console/consoleStore';

const PATHS: Record<string, string> = {
  admin_users: 'permissions.admin_users',
  grant_model: 'permissions.capabilities.model',
  grant_clear_memory: 'permissions.capabilities.clear_memory',
};

const GLOBAL_CAP: QqCapability = {
  key: 'model',
  name: '切换模型',
  desc: '查看和切换当前对话模型。',
  default: 'admin',
  group_scoped: false,
  grants: ['off', 'admin'],
};

const SCOPED_CAP: QqCapability = {
  key: 'clear_memory',
  name: '清空群记忆',
  desc: '清掉某个群的对话记忆。',
  default: 'admin',
  group_scoped: true,
  grants: ['off', 'admin', 'owner', 'group_admin'],
};

// bot 公布的号码规则，逐字照抄 live_config 的 ID_INPUT_RULE。
const ID_RULE: QqInputRule = { kind: 'id', pattern: '^[0-9]+$', min: 1, max: 9007199254740991 };

// inputRules 传 null = 旧 bot，整个 input_rules 字段都不在响应里。
const seed = (
  capabilities: QqCapability[] | undefined,
  values: Record<string, unknown> = { admin_users: [10001] },
  inputRules: Record<string, QqInputRule> | null = { admin_users: ID_RULE },
) => {
  useConsoleStore.setState({
    live: {
      published: true,
      applied: true,
      file: { exists: true, mtime_ns: 1, size: 1 },
      state: { values, paths: PATHS, capabilities, input_rules: inputRules ?? undefined },
    },
    draft: {},
  });
};

const rowFor = (name: string) => screen.getByText(name).closest('li') as HTMLElement;

beforeEach(() => {
  useConsoleStore.setState({ draft: {} });
});

describe('the capability list comes from the bot', () => {
  it('renders whatever the bot published', () => {
    seed([GLOBAL_CAP, SCOPED_CAP]);

    render(<PermissionsPage />);

    expect(screen.getByText('切换模型')).toBeInTheDocument();
    expect(screen.getByText('清空群记忆')).toBeInTheDocument();
  });

  it('renders a capability the frontend has never heard of', () => {
    // 后端加一项能力不该要求前端跟着改，漏掉的那项会在界面上无声缺席。
    seed([{ ...GLOBAL_CAP, key: 'brand_new', name: '新能力', desc: '以后才有的' }]);

    render(<PermissionsPage />);

    expect(screen.getByText('新能力')).toBeInTheDocument();
  });

  it('says so instead of inventing a list when the bot published none', () => {
    seed(undefined);

    render(<PermissionsPage />);

    expect(screen.getByText(/还没公布能力清单/)).toBeInTheDocument();
  });
});

describe('the grant options match what the capability can honour', () => {
  it('offers a global capability only off and admin', () => {
    seed([GLOBAL_CAP]);

    render(<PermissionsPage />);

    const buttons = within(rowFor('切换模型')).getAllByRole('button');
    expect(buttons.map((b) => b.textContent)).toEqual(['不开放', '仅管理员']);
  });

  it('offers a scoped capability the group roles as well', () => {
    seed([SCOPED_CAP]);

    render(<PermissionsPage />);

    const buttons = within(rowFor('清空群记忆')).getAllByRole('button');
    expect(buttons.map((b) => b.textContent)).toEqual(['不开放', '仅管理员', '加群主', '加群管']);
  });

  it('shows an unknown grant by its raw name rather than dropping it', () => {
    seed([{ ...SCOPED_CAP, grants: ['admin', 'moderator'] }]);

    render(<PermissionsPage />);

    expect(within(rowFor('清空群记忆')).getByText('moderator')).toBeInTheDocument();
  });
});

describe('the current grant', () => {
  it('marks the published value as pressed', () => {
    seed([SCOPED_CAP], { admin_users: [10001], grant_clear_memory: 'owner' });

    render(<PermissionsPage />);

    expect(within(rowFor('清空群记忆')).getByRole('button', { name: '加群主' }))
      .toHaveAttribute('aria-pressed', 'true');
  });

  it('falls back to the published default when the key is absent', () => {
    seed([SCOPED_CAP]);

    render(<PermissionsPage />);

    expect(within(rowFor('清空群记忆')).getByRole('button', { name: '仅管理员' }))
      .toHaveAttribute('aria-pressed', 'true');
  });

  it('writes the draft under the live_config key name', () => {
    // 草稿按键名存，点分路径要到写 yaml 时才换 —— 这里存错了会写进一个没人读的路径。
    seed([SCOPED_CAP]);
    render(<PermissionsPage />);

    fireEvent.click(within(rowFor('清空群记忆')).getByRole('button', { name: '加群管' }));

    expect(useConsoleStore.getState().draft).toEqual({ grant_clear_memory: 'group_admin' });
  });

  it('reflects the draft rather than the published value after a click', () => {
    seed([SCOPED_CAP], { admin_users: [10001], grant_clear_memory: 'off' });
    render(<PermissionsPage />);

    fireEvent.click(within(rowFor('清空群记忆')).getByRole('button', { name: '加群主' }));

    expect(within(rowFor('清空群记忆')).getByRole('button', { name: '加群主' }))
      .toHaveAttribute('aria-pressed', 'true');
  });
});

describe('the scope marker', () => {
  it('stays away while the capability sits with the roster', () => {
    seed([SCOPED_CAP]);

    render(<PermissionsPage />);

    expect(within(rowFor('清空群记忆')).queryByText('只在本群')).not.toBeInTheDocument();
  });

  it('appears once a group role gets the capability', () => {
    seed([SCOPED_CAP], { admin_users: [10001], grant_clear_memory: 'owner' });

    render(<PermissionsPage />);

    expect(within(rowFor('清空群记忆')).getByText('只在本群')).toBeInTheDocument();
  });

  it('never appears on a capability that is not scoped', () => {
    // 全局能力的档位里压根没有群主，标一个「只在本群」是在描述一个不存在的边界。
    seed([{ ...GLOBAL_CAP, grants: ['off', 'admin', 'owner'] }], {
      admin_users: [10001], grant_model: 'owner',
    });

    render(<PermissionsPage />);

    expect(within(rowFor('切换模型')).queryByText('只在本群')).not.toBeInTheDocument();
  });
});

describe('the empty roster warning', () => {
  it('uses the note the bot published rather than a copy of it', () => {
    useConsoleStore.setState({
      live: {
        published: true,
        applied: true,
        file: { exists: true, mtime_ns: 1, size: 1 },
        state: {
          values: { admin_users: [] },
          paths: PATHS,
          notes: { admin_users: '空列表 = 管理命令全部不可用' },
          capabilities: [GLOBAL_CAP],
        },
      },
      draft: {},
    });

    render(<PermissionsPage />);

    expect(screen.getByText('空列表 = 管理命令全部不可用')).toBeInTheDocument();
  });

  it('stays quiet once someone is on the roster', () => {
    seed([GLOBAL_CAP]);

    render(<PermissionsPage />);

    expect(screen.queryByText(/名单为空/)).not.toBeInTheDocument();
  });
});

describe('the roster input waits for the rule the bot publishes', () => {
  const rosterInput = () => screen.getByRole('textbox') as HTMLInputElement;

  it('takes a number once the rule is published', () => {
    seed([GLOBAL_CAP], { admin_users: [10001] });
    render(<PermissionsPage />);

    fireEvent.change(rosterInput(), { target: { value: '10002' } });
    fireEvent.keyDown(rosterInput(), { key: 'Enter' });

    expect(useConsoleStore.getState().draft).toEqual({ admin_users: [10001, 10002] });
  });

  it('refuses a number the published rule rejects and says so', () => {
    seed([GLOBAL_CAP], { admin_users: [10001] });
    render(<PermissionsPage />);

    fireEvent.change(rosterInput(), { target: { value: '-500' } });
    fireEvent.keyDown(rosterInput(), { key: 'Enter' });

    expect(useConsoleStore.getState().draft).toEqual({});
    expect(screen.getByText('只能填数字号码')).toBeInTheDocument();
  });

  it('tells apart a number already on the roster', () => {
    seed([GLOBAL_CAP], { admin_users: [10001] });
    render(<PermissionsPage />);

    fireEvent.change(rosterInput(), { target: { value: '10001' } });
    fireEvent.keyDown(rosterInput(), { key: 'Enter' });

    expect(screen.getByText('这个号码已经在名单里了')).toBeInTheDocument();
  });

  it('disables itself when the bot published no rule at all', () => {
    // 旧 bot 配新前端。此时按前端猜的规则收下输入正是漂移本身，所以宁可不收。
    seed([GLOBAL_CAP], { admin_users: [10001] }, null);

    render(<PermissionsPage />);

    expect(rosterInput()).toBeDisabled();
    expect(rosterInput().placeholder).toMatch(/没公布/);
  });

  it('still lists and removes what is already on the roster', () => {
    // 禁用的只是新增。名单里那些人必须还能被移出去，否则连回退都做不到。
    seed([GLOBAL_CAP], { admin_users: [10001] }, null);
    render(<PermissionsPage />);

    fireEvent.click(screen.getByLabelText('移出名单 10001'));

    expect(useConsoleStore.getState().draft).toEqual({ admin_users: [] });
  });
});
