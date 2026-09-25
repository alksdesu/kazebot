import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import yaml from 'js-yaml';
import * as api from '../api/supervisorClient';
import { AgentsSettingsPage } from '../components/settings/pages/AgentsSettingsPage';
import { SkillsSettingsPage } from '../components/settings/pages/SkillsSettingsPage';
import { ToolsSettingsPage } from '../components/settings/pages/ToolsSettingsPage';
import { AutomationSettingsPage } from '../components/settings/pages/AutomationSettingsPage';
import { SkillsSettingsRightPanel, McpSettingsRightPanel } from '../components/settings/panels/SettingsContextPanels';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

vi.mock('../features/execution', () => ({ RemindersPage: () => null }));
vi.mock('../components/settings/pages/NodeGrantsSection', () => ({ NodeGrantsSection: () => null }));
vi.mock('../api/supervisorClient', async importOriginal => ({
  ...await importOriginal<typeof import('../api/supervisorClient')>(),
  getNodes: vi.fn(), getSkills: vi.fn(), getTools: vi.fn(), getAllToolNames: vi.fn(),
  createNode: vi.fn(), createSkill: vi.fn(), createTool: vi.fn(),
  getSchedulesRaw: vi.fn(), updateSchedulesRaw: vi.fn(), getSkillRaw: vi.fn(), updateSkillRaw: vi.fn(),
  getMcpClientsRaw: vi.fn(), updateMcpClientsRaw: vi.fn(),
}));
function deferred<T>() {
  let resolve!: (value: T) => void; let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}
beforeEach(() => {
  vi.resetAllMocks(); vi.spyOn(window, 'confirm').mockReturnValue(true);
  useSettingsStore.setState({ adminToken: 'synthetic', isAuthenticated: true });
  useSettingsSelectionStore.setState({ selectedNode: null, selectedTool: null, selectedSkill: null, selectedScheduleId: null, selectedMcpClient: null });
  vi.mocked(api.getNodes).mockResolvedValue([]); vi.mocked(api.getSkills).mockResolvedValue([]);
  vi.mocked(api.getTools).mockResolvedValue([]); vi.mocked(api.getAllToolNames).mockResolvedValue([]);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it.each([
  { Page: AgentsSettingsPage, action: '创建节点', label: '创建节点', mutate: 'createNode' as const, load: 'getNodes' as const },
  { Page: SkillsSettingsPage, action: '创建技能', label: '创建技能', mutate: 'createSkill' as const, load: 'getSkills' as const },
  { Page: ToolsSettingsPage, action: '创建工具', label: '创建工具', mutate: 'createTool' as const, load: 'getTools' as const },
])('$action freezes inputs and submits once, keeping the new name after failure', async ({ Page, action, label, mutate, load }) => {
  const request = deferred<unknown>(); vi.mocked(api[mutate]).mockReturnValue(request.promise);
  render(<Page />);
  await waitFor(() => expect(api[load]).toHaveBeenCalled());
  fireEvent.change(screen.getByLabelText(label), { target: { value: 'new_resource' } });
  const button = screen.getByRole('button', { name: action });
  fireEvent.click(button); fireEvent.click(button);
  expect(api[mutate]).toHaveBeenCalledOnce();
  expect(button).toBeDisabled(); expect(screen.getByLabelText(label)).toBeDisabled();
  await act(async () => request.reject(new Error('synthetic failure')));
  expect(screen.getByLabelText(label)).toHaveValue('new_resource');
  expect(screen.getByText('synthetic failure')).toBeInTheDocument();
});

it('creates a disabled schedule and preserves existing ownership and root metadata', async () => {
  let raw = 'root_extra: preserve\nschedules:\n - id: owned\n   created_by: qq:123\n';
  vi.mocked(api.getSchedulesRaw).mockImplementation(async () => raw);
  vi.mocked(api.updateSchedulesRaw).mockImplementation(async (_, next) => { raw = next; });
  render(<AutomationSettingsPage />);
  await screen.findByText('owned');
  fireEvent.change(screen.getByLabelText('创建定时任务'), { target: { value: 'new_schedule' } });
  fireEvent.click(screen.getByRole('button', { name: '创建任务' }));
  await waitFor(() => expect(api.updateSchedulesRaw).toHaveBeenCalledOnce());
  expect(yaml.load(raw)).toMatchObject({ root_extra: 'preserve', schedules: [
    { id: 'owned', created_by: 'qq:123' }, { id: 'new_schedule', enabled: false },
  ] });
  expect(await screen.findByText('任务已创建但尚未启用，请在右栏填写内容后启用。')).toBeInTheDocument();
});

it('does not create over a structurally invalid schedules document', async () => {
  vi.mocked(api.getSchedulesRaw).mockResolvedValue('schedules: {not: a list}');
  render(<AutomationSettingsPage />);
  await screen.findByText(/schedules 必须/);
  fireEvent.change(screen.getByLabelText('创建定时任务'), { target: { value: 'new_schedule' } });
  fireEvent.click(screen.getByRole('button', { name: '创建任务' }));
  await waitFor(() => expect(api.getSchedulesRaw).toHaveBeenCalledTimes(2));
  expect(api.updateSchedulesRaw).not.toHaveBeenCalled();
});

it('refreshes skill quick fields after raw save and preserves unknown frontmatter and Markdown', async () => {
  vi.mocked(api.getSkillRaw).mockResolvedValue('---\nname: test\nstrategy: normal\nkeywords: []\n---\nbody\n');
  vi.mocked(api.updateSkillRaw).mockResolvedValue(undefined);
  useSettingsSelectionStore.setState({ selectedSkill: { name: 'test', enabled: true, strategy: 'normal', keywords: [] } });
  render(<SkillsSettingsRightPanel />);
  const raw = await screen.findByLabelText('技能 Raw Markdown 编辑器');
  await waitFor(() => expect((raw as HTMLTextAreaElement).value).toContain('name: test'));
  fireEvent.click(screen.getByText('Raw Markdown 编辑（高级）'));
  fireEvent.change(raw, { target: { value: '---\nname: test\nstrategy: constant\nkeywords: ["x,y"]\nunknown: keep\n---\nbody unchanged\n' } });
  fireEvent.click(screen.getByRole('button', { name: '保存 Raw Markdown' }));
  await waitFor(() => expect(screen.getByLabelText('策略')).toHaveValue('constant'));
  fireEvent.change(screen.getByLabelText('order'), { target: { value: '2' } });
  fireEvent.click(screen.getByRole('button', { name: '保存快捷字段' }));
  await waitFor(() => expect(api.updateSkillRaw).toHaveBeenCalledTimes(2));
  const sent = vi.mocked(api.updateSkillRaw).mock.calls[1][2];
  expect(yaml.load(sent.split('---')[1])).toMatchObject({ keywords: ['x,y'] });
  expect(sent).toContain('strategy: constant'); expect(sent).toContain('unknown: keep'); expect(sent).toContain('body unchanged');
});

it('keeps commas and newlines in MCP arguments when only description is edited', async () => {
  const args = ['one,two', 'three\nfour'];
  vi.mocked(api.getMcpClientsRaw).mockResolvedValue(yaml.dump({ clients: { a: { transport: 'stdio', command: 'synthetic', args } } }));
  vi.mocked(api.updateMcpClientsRaw).mockResolvedValue(undefined);
  useSettingsSelectionStore.setState({ selectedMcpClient: { id: 'a', transport: 'stdio', enabled: true } });
  render(<McpSettingsRightPanel />);
  fireEvent.change(await screen.findByLabelText('description'), { target: { value: 'new description' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(api.updateMcpClientsRaw).toHaveBeenCalledOnce());
  expect(yaml.load(vi.mocked(api.updateMcpClientsRaw).mock.calls[0][1])).toMatchObject({ clients: { a: { args, description: 'new description' } } });
});
