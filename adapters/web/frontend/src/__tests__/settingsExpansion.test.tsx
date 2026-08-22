// [2026-06-02] Tests for the expanded settings area.
// Why: the settings task adds several registry entries, API wrappers, and inferred
// tool risk rules at once. How: assert the public contracts that pages and client
// preferences depend on before implementing the UI. Purpose: future changes can
// refactor the implementation without silently dropping a tab or endpoint wrapper.
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createNode,
  createSkill,
  createTool,
  deleteNode,
  deleteSkill,
  deleteTool,
  getMcpClientsRaw,
  getNodeRaw,
  getPolicyRaw,
  getRuntimeRaw,
  getSchedulesRaw,
  getSkillRaw,
  getToolRaw,
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
import { settingsTabs } from '../components/settings/settingsTabs';
import { inferToolRisk } from '../utils/toolRisk';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('expanded settings registry', () => {
  it('registers all P0 and P1 settings tabs in order', () => {
    expect(settingsTabs.map((tab) => tab.id)).toEqual([
      'general',
      'client',
      'runtime',
      'system',
      'approvals',
      'agents',
      'node-files',
      'tools',
      'drawtools',
      'skills',
      'mcp',
      'automation',
      'advanced',
    ]);
    expect(settingsTabs.find((tab) => tab.id === 'system')).toMatchObject({ label: '系统', icon: 'settings_power', order: 4 });
    expect(settingsTabs.find((tab) => tab.id === 'tools')).toMatchObject({ label: '工具与权限', icon: 'build', order: 8 });
  });

  it('每个 tab 都归了组，侧栏才摆得下二十多项', () => {
    expect(settingsTabs.every((tab) => tab.group)).toBe(true);
  });
});

describe('tool risk inference', () => {
  it('classifies read-like and MCP tools as low risk', () => {
    expect(inferToolRisk('read_file')).toBe('low');
    expect(inferToolRisk('list_dir')).toBe('low');
    expect(inferToolRisk('search_in_files')).toBe('low');
    expect(inferToolRisk('get_admin_state')).toBe('low');
    expect(inferToolRisk('mcp_github_search_code')).toBe('low');
  });

  it('classifies command and restart tools as high risk', () => {
    expect(inferToolRisk('execute_command')).toBe('high');
    expect(inferToolRisk('restart_engine')).toBe('high');
    expect(inferToolRisk('remote_exec')).toBe('high');
    expect(inferToolRisk('request_restart')).toBe('high');
  });

  it('classifies write-like and unknown tools as medium risk', () => {
    expect(inferToolRisk('write_file')).toBe('medium');
    expect(inferToolRisk('apply_diff')).toBe('medium');
    expect(inferToolRisk('delete_node')).toBe('medium');
    expect(inferToolRisk('create_skill')).toBe('medium');
    expect(inferToolRisk('custom_tool')).toBe('medium');
  });
});

describe('expanded settings API wrappers', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('unwraps raw content responses and sends raw content payloads', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'PUT') return jsonResponse({ ok: true });
      if (url.endsWith('/raw')) return jsonResponse({ content: 'value: 1\n' });
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(getRuntimeRaw('token')).resolves.toBe('value: 1\n');
    await expect(getPolicyRaw('token')).resolves.toBe('value: 1\n');
    await expect(getSchedulesRaw('token')).resolves.toBe('value: 1\n');
    await expect(getMcpClientsRaw('token')).resolves.toBe('value: 1\n');
    await expect(getNodeRaw('token', 'ereuna_main')).resolves.toBe('value: 1\n');
    await expect(getToolRaw('token', 'read_file')).resolves.toBe('value: 1\n');
    await expect(getSkillRaw('token', 'jina-reader')).resolves.toBe('value: 1\n');

    await updateRuntimeRaw('token', 'runtime: true\n');
    await updatePolicyRaw('token', 'policy: true\n');
    await updateSchedulesRaw('token', 'schedules: []\n');
    await updateMcpClientsRaw('token', 'clients: {}\n');
    await updateNodeRaw('token', 'ereuna_main', 'id: ereuna_main\n');
    await updateToolRaw('token', 'read_file', 'print("ok")\n');
    await updateSkillRaw('token', 'jina-reader', '# Skill\n');

    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/runtime/raw', expect.objectContaining({ headers: expect.objectContaining({ Authorization: 'Bearer token' }) }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/runtime/raw', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ content: 'runtime: true\n' }),
    }));
  });

  it('uses the expected create delete reload and restart endpoints', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true, scheduled: true, target: 'engine' }));
    vi.stubGlobal('fetch', fetchMock);

    await createNode('token', { id: 'new_node', content: 'id: new_node\n' });
    await createTool('token', { id: 'new_tool', content: 'SPEC = {}\n' });
    await createSkill('token', { id: 'new_skill', content: '# Skill\n' });
    await deleteNode('token', 'new_node');
    await deleteTool('token', 'new_tool');
    await deleteSkill('token', 'new_skill');
    await reloadConfig('token');
    await reloadTools('token');
    await restartEngine('token');

    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/nodes', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/tools/new_tool', expect.objectContaining({ method: 'DELETE' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/config/reload', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/tools/reload', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/restart', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ target: 'engine', reason: '用户从设置页面请求重启引擎' }),
    }));
  });
});
