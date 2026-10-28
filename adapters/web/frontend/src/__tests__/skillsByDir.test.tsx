// 技能按目录认：frontmatter 的 name 可以重名、可以和目录名不一样，读、写、删都得按目录找文件。
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { deleteSkill, getSkillRaw, getSkills, updateSkillRaw, type AdminSkill } from '../api/supervisorClient';
import { SkillsSettingsPage } from '../components/settings/pages/SkillsSettingsPage';
import { SkillsSettingsRightPanel } from '../components/settings/panels/SettingsContextPanels';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getSkills: vi.fn(),
  getSkillRaw: vi.fn(),
  updateSkillRaw: vi.fn(),
  deleteSkill: vi.fn(),
}));

const SKILLS: AdminSkill[] = [
  { name: 'shared', dir: 'alpha', path: 'config/skills/alpha/SKILL.md', enabled: true, strategy: 'normal', keywords: [] },
  { name: 'shared', dir: 'beta', path: 'config/skills/beta/SKILL.md', enabled: true, strategy: 'normal', keywords: [] },
  { name: 'broken', dir: 'broken', path: 'config/skills/broken/SKILL.md', problem: '第 2 行：frontmatter 不是一张表' },
  { name: 'rule', dir: 'rule', path: 'config/skills/rule/SKILL.md', enabled: true, strategy: 'output', keywords: [], rule_error: 'trigger 不是能编译的正则' },
];

const SKILL_MD = '---\nname: shared\nstrategy: normal\nkeywords: []\n---\nbody\n';

beforeEach(() => {
  vi.resetAllMocks();
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true, rightPanelOpen: false });
  useSettingsSelectionStore.setState({ selectedNode: null, selectedTool: null, selectedSkill: null, selectedScheduleId: null, selectedMcpClient: null });
  vi.mocked(getSkills).mockResolvedValue(SKILLS);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const sharedRows = () => screen.findAllByRole('button', { name: /shared/ });

describe('技能列表', () => {
  it('两个目录写了同一个 name，各是各的一行，点哪行选的就是哪个目录', async () => {
    render(<SkillsSettingsPage />);

    const rows = await sharedRows();
    expect(rows).toHaveLength(2);
    expect(within(rows[1]).getByText('beta/')).toBeInTheDocument();

    fireEvent.click(rows[1]);

    expect(useSettingsSelectionStore.getState().selectedSkill?.dir).toBe('beta');
  });

  it('删除按目录找文件', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(deleteSkill).mockResolvedValue({ ok: true });
    render(<SkillsSettingsPage />);

    fireEvent.click((await sharedRows())[1]);
    fireEvent.click(screen.getByRole('button', { name: '删除选中技能' }));

    await waitFor(() => expect(deleteSkill).toHaveBeenCalledWith('admin-token', 'beta'));
    expect(window.confirm).toHaveBeenCalledWith('确定要删除技能目录 beta 吗？');
  });

  it('用不了的技能标出来并说清问题，不再显示启用', async () => {
    // 加载时整个技能被跳过，再显示「启用」就是在骗人。
    render(<SkillsSettingsPage />);

    const row = await screen.findByRole('button', { name: /broken/ });

    expect(within(row).getByText('用不了')).toBeInTheDocument();
    expect(within(row).getByText('技能文件用不了，选中后在右栏修好：第 2 行：frontmatter 不是一张表')).toBeInTheDocument();
    expect(within(row).queryByText('启用')).toBeNull();
  });

  it('输出规则写得不对时说出来', async () => {
    render(<SkillsSettingsPage />);

    const row = await screen.findByRole('button', { name: /rule/ });

    expect(within(row).getByText('输出规则不生效：trigger 不是能编译的正则')).toBeInTheDocument();
    expect(within(row).getByText('启用')).toBeInTheDocument();
  });
});

describe('技能右栏', () => {
  it('按目录读文件、存文件', async () => {
    vi.mocked(getSkillRaw).mockResolvedValue({ content: SKILL_MD, hash: 'skill-1' });
    vi.mocked(updateSkillRaw).mockResolvedValue({ ok: true, hash: 'skill-2' });
    useSettingsSelectionStore.setState({ selectedSkill: SKILLS[1] });
    render(<SkillsSettingsRightPanel />);

    const raw = await screen.findByLabelText('技能 Raw Markdown 编辑器');
    await waitFor(() => expect((raw as HTMLTextAreaElement).value).toContain('name: shared'));
    expect(getSkillRaw).toHaveBeenCalledWith('admin-token', 'beta');
    expect(screen.getByText('目录：').nextSibling).toHaveTextContent('beta');

    fireEvent.click(screen.getByRole('button', { name: '保存技能' }));

    await waitFor(() => expect(updateSkillRaw).toHaveBeenCalledWith('admin-token', 'beta', expect.any(String), 'skill-1'));
  });

  it('刚建好、还没带 dir 的技能按 name 找', async () => {
    vi.mocked(getSkillRaw).mockResolvedValue({ content: SKILL_MD, hash: 'skill-1' });
    useSettingsSelectionStore.setState({ selectedSkill: { name: 'shared', enabled: true, strategy: 'normal', keywords: [] } });
    render(<SkillsSettingsRightPanel />);

    await waitFor(() => expect(getSkillRaw).toHaveBeenCalledWith('admin-token', 'shared'));
    expect(screen.queryByText('目录：')).toBeNull();
  });
});
