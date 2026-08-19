// [2026-06-02] Tests for the first admin infrastructure batch.
// Why: this batch adds API wrappers, a shared raw editor, and tool-risk helpers
// without adding settings pages. How: assert endpoint contracts, editor styling, and
// risk labels/classes directly. Purpose: later tab pages can rely on these stable
// shared primitives instead of re-validating backend wiring in each page test.
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createNode,
  createSkill,
  createTool,
  deleteNode,
  deleteSkill,
  deleteTool,
  getAdminState,
  getAllToolNames,
  getMcpClients,
  getMcpClientsRaw,
  getNodeRaw,
  getPolicyRaw,
  getRuntimeRaw,
  getSchedulesRaw,
  getSkillRaw,
  getSkills,
  getToolRaw,
  getTools,
  reloadConfig,
  reloadTools,
  restartEngine,
  updateMcpClientsRaw,
  updateNodeRaw,
  updatePolicyRaw,
  updateRuntimeRaw,
  updateSchedulesRaw,
  updateSkillRaw,
  updateToolRaw,
} from '../api/supervisorClient';
import { YamlEditor } from '../components/common';
import { inferToolRisk, riskClassName, riskLabel } from '../utils/toolRisk';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('admin API infrastructure wrappers', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses bearer-authenticated system and collection endpoints', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/tools') || url.endsWith('/skills') || url.endsWith('/mcp-clients') || url.endsWith('/all-tool-names')) {
        return jsonResponse([]);
      }
      return jsonResponse({ ok: true, sessions: 0, approvals: {}, tasks: {}, pending_approvals: [], engine_runtime: {} });
    });
    vi.stubGlobal('fetch', fetchMock);

    await getAdminState('admin-token');
    await restartEngine('admin-token');
    await reloadConfig('admin-token');
    await getTools('admin-token');
    await reloadTools('admin-token');
    await getAllToolNames('admin-token');
    await getSkills('admin-token');
    await getMcpClients('admin-token');

    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/state', expect.objectContaining({ headers: { Authorization: 'Bearer admin-token' } }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/restart', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/config/reload', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/tools', expect.objectContaining({ headers: { Authorization: 'Bearer admin-token' } }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/tools/reload', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/all-tool-names', expect.objectContaining({ headers: { Authorization: 'Bearer admin-token' } }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/skills', expect.objectContaining({ headers: { Authorization: 'Bearer admin-token' } }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/mcp-clients', expect.objectContaining({ headers: { Authorization: 'Bearer admin-token' } }));
  });

  it('unwraps raw content endpoints and writes content payloads', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'PUT') return jsonResponse({ ok: true });
      return jsonResponse({ content: 'value: 1\n' });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(getRuntimeRaw('admin-token')).resolves.toBe('value: 1\n');
    await expect(getPolicyRaw('admin-token')).resolves.toBe('value: 1\n');
    await expect(getSchedulesRaw('admin-token')).resolves.toBe('value: 1\n');
    await expect(getNodeRaw('admin-token', 'node/a')).resolves.toBe('value: 1\n');
    await expect(getToolRaw('admin-token', 'tool/a')).resolves.toBe('value: 1\n');
    await expect(getSkillRaw('admin-token', 'skill/a')).resolves.toBe('value: 1\n');
    await expect(getMcpClientsRaw('admin-token')).resolves.toBe('value: 1\n');

    await updateRuntimeRaw('admin-token', 'runtime: true\n');
    await updatePolicyRaw('admin-token', 'policy: true\n');
    await updateSchedulesRaw('admin-token', 'schedules: []\n');
    await updateNodeRaw('admin-token', 'node/a', 'id: node/a\n');
    await updateToolRaw('admin-token', 'tool/a', 'print("ok")\n');
    await updateSkillRaw('admin-token', 'skill/a', '# Skill\n');
    await updateMcpClientsRaw('admin-token', 'clients: {}\n');

    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/nodes/node%2Fa/raw', expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/tools/tool%2Fa/raw', expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/skills/skill%2Fa/raw', expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/runtime/raw', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ content: 'runtime: true\n' }),
    }));
  });

  it('creates and deletes nodes, tools, and skills through admin config endpoints', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);

    await createNode('admin-token', { id: 'node-a', content: 'id: node-a\n' });
    await createTool('admin-token', { id: 'tool-a', content: 'SPEC = {}\n' });
    await createSkill('admin-token', { id: 'skill-a', content: '# Skill\n' });
    await deleteNode('admin-token', 'node-a');
    await deleteTool('admin-token', 'tool-a');
    await deleteSkill('admin-token', 'skill-a');

    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/nodes', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/tools', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/skills', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/nodes/node-a', expect.objectContaining({ method: 'DELETE' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/tools/tool-a', expect.objectContaining({ method: 'DELETE' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/skills/skill-a', expect.objectContaining({ method: 'DELETE' }));
  });
});

describe('YamlEditor', () => {
  it('renders a controlled monospace textarea with the requested default height and theme classes', () => {
    const onChange = vi.fn();
    render(<YamlEditor onChange={onChange} placeholder="编辑 YAML" value="a: 1" />);

    const editor = screen.getByPlaceholderText('编辑 YAML');
    expect(editor).toHaveClass('bg-[var(--duties-bg)]');
    expect(editor).toHaveClass('border-[var(--duties-border)]');
    expect(editor).toHaveClass('text-[var(--duties-text)]');
    expect(editor).toHaveClass('font-mono');
    expect(editor).toHaveClass('text-xs');
    expect(editor).toHaveClass('resize-y');
    expect(editor).toHaveStyle({ height: '300px' });

    fireEvent.change(editor, { target: { value: 'b: 2' } });
    expect(onChange).toHaveBeenCalledWith('b: 2');
  });

  it('honors read-only mode and custom CSS height', () => {
    render(<YamlEditor height="12rem" onChange={() => undefined} readOnly value="a: 1" />);

    const editor = screen.getByRole('textbox');
    expect(editor).toHaveAttribute('readonly');
    expect(editor).toHaveStyle({ height: '12rem' });
  });
});

describe('tool risk helpers', () => {
  it('infers risk levels and returns the shared Chinese labels and badge classes', () => {
    expect(inferToolRisk('read_file')).toBe('low');
    expect(inferToolRisk('mcp_github_search_code')).toBe('low');
    expect(inferToolRisk('execute_command')).toBe('high');
    expect(inferToolRisk('request_restart')).toBe('high');
    expect(inferToolRisk('write_file')).toBe('medium');
    expect(inferToolRisk('unknown_tool')).toBe('medium');

    expect(riskLabel('low')).toBe('低风险');
    expect(riskLabel('medium')).toBe('中风险');
    expect(riskLabel('high')).toBe('高风险');

    expect(riskClassName('low')).toBe('border-green-200 bg-green-50 text-green-700');
    expect(riskClassName('medium')).toBe('border-orange-200 bg-orange-50 text-orange-700');
    expect(riskClassName('high')).toBe('border-red-200 bg-red-50 text-red-700');
  });
});
