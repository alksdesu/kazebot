import { confirmNavigation } from '../../../hooks/useUnsavedChanges';
import { useExclusiveAction } from '../../../hooks/useExclusiveAction';
// [2026-06-02] Skill management settings page.
// Why: skills are Markdown files with frontmatter that operators frequently adjust.
// How: list parsed skill metadata and support create/delete actions here, while the
// full frontmatter form and Markdown editor live in the Settings right panel. Purpose:
// skill maintenance is split into a clean list and a focused editor.
import { useEffect, useState } from 'react';

import { createSkill, deleteSkill, getSkills, receipted, skillDir, type AdminSkill } from '../../../api/supervisorClient';
import { usePageNotice } from '../../../hooks/usePageNotice';
import { useSettingsSelectionStore } from '../../../store/settingsSelectionStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { useRequestGuard } from '../../../hooks/useRequestGuard';
import { Block, Button, Feedback, Field, Input, PageShell } from './settingsControls';
import { savedMessage } from '../../../utils/sentence';
import { errorMessage } from '../../../api/request';

function defaultSkillMarkdown(name: string): string {
  // [2026-06-02] Provide a complete starter SKILL.md with frontmatter. Why: the
  // backend create endpoint writes raw content and expects users to supply metadata.
  // How: include enabled, strategy, keywords, order, priority, and scan_depth fields.
  // Purpose: new skills appear correctly and can be refined immediately in the right panel.
  return `---\nname: ${name}\ndescription: 通过设置页面创建的技能。\nenabled: true\nstrategy: normal\nkeywords: []\norder: 0\npriority: 0\nscan_depth: 0\n---\n\n# ${name}\n\n请在这里编写技能内容。\n`;
}

export const SkillsSettingsPage = () => {
  // [2026-06-02] Pull the right-panel opener into the list page. Why: selecting a
  // skill on mobile should reveal the skill editor immediately. How: call the shared
  // settings-store setter from each row click. Purpose: users do not need a second tap
  // on the small header chevron after choosing an item.
  const { adminToken, isAuthenticated, setRightPanelOpen } = useSettingsStore();
  const { selectedSkill, setSelectedSkill } = useSettingsSelectionStore();
  const [skills, setSkills] = useState<AdminSkill[]>([]);
  const [newName, setNewName] = useState('');
  const { notice, error, setMessage, setError, setLoadError, clearNotice } = usePageNotice();
  const [loading, setLoading] = useState(false);
  const { busy, run } = useExclusiveAction();
  const requests = useRequestGuard();

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    const request = requests.begin('skills');
    setLoading(true);
    try {
      const items = await getSkills(adminToken);
      if (!request.isCurrent()) return;
      setSkills(items);
      if (selectedSkill && !items.some((item) => skillDir(item) === skillDir(selectedSkill))) setSelectedSkill(null);
      setLoadError('');
    } catch (caught) {
      if (!request.isCurrent()) return;
      setLoadError(errorMessage(caught, '加载技能失败'));
    } finally {
      if (request.isCurrent()) setLoading(false);
    }
  };

  // Clear the notice only on an explicit refresh: create and delete reload right after writing theirs.
  const refresh = async () => {
    clearNotice();
    await load();
  };

  useEffect(() => { void refresh(); }, [requests]);

  useEffect(() => {
    // [2026-06-02] Refresh parsed skill metadata after the right-panel editor saves.
    // Why: the page no longer owns the raw Markdown state. How: listen for a local
    // browser event emitted by the right panel. Purpose: enabled, strategy, keywords,
    // and description previews update without a full settings reload.
    const handler = () => { void load(); };
    window.addEventListener('settings:skills-updated', handler);
    return () => window.removeEventListener('settings:skills-updated', handler);
  }, [requests, selectedSkill ? skillDir(selectedSkill) : '']);

  const create = () => run(async () => {
    if (!adminToken) return;
    const name = newName.trim();
    if (!name) { setError('请输入技能名称'); return; }
    if (!confirmNavigation()) return;
    try {
      const { result, receiptProblem } = await receipted(createSkill(adminToken, { id: name, content: defaultSkillMarkdown(name) }));
      if (!requests.isCurrent()) return;
      setNewName('');
      setMessage(savedMessage('技能已创建，请在右栏编辑 frontmatter 和正文。', '', result?.warnings), receiptProblem);
      await load();
      if (!requests.isCurrent()) return;
      setSelectedSkill({ name, description: '通过设置页面创建的技能。', enabled: true, strategy: 'normal', keywords: [] });
    } catch (caught) {
      if (!requests.isCurrent()) return;
      setError(errorMessage(caught, '创建技能失败'));
    }
  });
  const remove = () => run(async () => {
    if (!adminToken || !selectedSkill) return;
    const dir = skillDir(selectedSkill);
    if (!window.confirm(`确定要删除技能目录 ${dir} 吗？`)) return;
    if (!confirmNavigation()) return;
    try {
      // 回执读不懂时目录也已经删了：照删完收尾，再交代回执的事。
      const { receiptProblem } = await receipted(deleteSkill(adminToken, dir));
      if (!requests.isCurrent()) return;
      setSelectedSkill(null);
      setMessage('技能已删除', receiptProblem);
      await load();
    } catch (caught) {
      if (!requests.isCurrent()) return;
      setError(errorMessage(caught, '删除技能失败'));
    }
  });
  return (
    <PageShell authed={isAuthenticated} description="管理 skills 目录下的 SKILL.md 文件。选择技能后，请在右栏编辑 frontmatter 和 Markdown 正文。" title="技能管理">
      <Block frame="boxed" title="技能列表" hint="列表展示技能名称、启用状态、策略和关键词预览。编辑器位于右栏。">
        <div className="mb-3 flex flex-wrap gap-2">
          <Button tone="quiet" disabled={loading || busy} onClick={() => void refresh()}>{loading ? '正在刷新…' : '刷新技能'}</Button>
          <Button tone="danger" disabled={busy || !selectedSkill} onClick={remove}>删除选中技能</Button>
        </div>
        <div className="max-h-[var(--duties-pane-height-tall)] space-y-2 overflow-y-auto">
          {skills.map((skill) => (
            <button className={`w-full border p-3 text-left ${selectedSkill && skillDir(selectedSkill) === skillDir(skill) ? 'border-[var(--duties-text)] bg-[var(--duties-bg)]' : 'border-[var(--duties-border)] bg-[var(--duties-bg)]'}`} key={skillDir(skill)} disabled={busy} onClick={() => { setSelectedSkill(skill); setMessage(''); setRightPanelOpen(true); }} type="button">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs font-semibold">{skill.name}</span>
                {skillDir(skill) !== skill.name && <span className="font-mono text-micro text-[var(--duties-tertiary)]">{skillDir(skill)}/</span>}
                {skill.problem ? (
                  <span className="border border-[var(--duties-danger)] px-1.5 py-0.5 text-nano text-[var(--duties-danger)]">用不了</span>
                ) : (
                  <>
                    <span className={`border px-1.5 py-0.5 text-nano ${skill.enabled === false ? 'border-[var(--duties-border)] text-[var(--duties-secondary)]' : 'border-[var(--duties-border)] text-[var(--duties-success)]'}`}>{skill.enabled === false ? '禁用' : '启用'}</span>
                    <span className="font-mono text-micro text-[var(--duties-tertiary)]">{skill.strategy || 'normal'}</span>
                  </>
                )}
              </div>
              {skill.problem ? (
                // 加载时整个技能被跳过：不注入，也不当输出规则。
                <p className="mt-1 text-xs text-[var(--duties-danger)]">技能文件用不了，选中后在右栏修好：{skill.problem}</p>
              ) : (
                <p className="mt-1 text-xs text-[var(--duties-secondary)]">{skill.description || skill.body_preview || '无描述'}</p>
              )}
              {skill.rule_error && <p className="mt-1 text-xs text-[var(--duties-danger)]">输出规则不生效：{skill.rule_error}</p>}
              {(skill.keywords || []).length > 0 && <p className="mt-1 truncate font-mono text-mini text-[var(--duties-tertiary)]">{(skill.keywords || []).join(', ')}</p>}
            </button>
          ))}
        </div>
        <div className="mt-4 border-t border-[var(--duties-border)] pt-3">
          <Field htmlFor="new-skill-name" label="创建技能" layout="stacked">
            <Input width="wide" disabled={busy} id="new-skill-name" onChange={(event) => setNewName(event.target.value)} placeholder="技能目录名" value={newName} />
            <Button className="mt-2" disabled={busy} onClick={create}>创建技能</Button>
          </Field>
        </div>
        <Feedback error={error} notice={notice} />
      </Block>
    </PageShell>
  );
};
