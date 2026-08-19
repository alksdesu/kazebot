// [2026-06-02] Settings list selection opens the contextual right panel.
// Why: on mobile, selecting an item should immediately reveal the editor instead of
// requiring a second header toggle. How: render each list page against mocked admin
// endpoints, click one row, and assert the shared right-panel store opens. Purpose:
// future list refactors preserve mobile discoverability for contextual editors.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AgentsSettingsPage } from '../components/settings/pages/AgentsSettingsPage';
import { AutomationSettingsPage } from '../components/settings/pages/AutomationSettingsPage';
import { McpSettingsPage } from '../components/settings/pages/McpSettingsPage';
import { SkillsSettingsPage } from '../components/settings/pages/SkillsSettingsPage';
import { ToolsSettingsPage } from '../components/settings/pages/ToolsSettingsPage';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function settingsYamlResponse(content: string): Response {
  return jsonResponse({ content });
}

function authenticateSettings() {
  // [2026-06-02] Seed authenticated admin state for isolated list-page tests.
  // Why: these pages intentionally hide rows until admin auth exists. How: set the
  // Zustand flags written by the login flow. Purpose: tests exercise selection behavior
  // without coupling to the General settings login form.
  useSettingsStore.setState({ adminToken: 'admin-token', isAuthenticated: true, rightPanelOpen: false });
}

describe('settings list pages open the right panel on selection', () => {
  beforeEach(() => {
    localStorage.clear();
    authenticateSettings();
    useSettingsSelectionStore.setState({
      selectedNode: null,
      selectedTool: null,
      selectedSkill: null,
      selectedMcpClient: null,
      selectedScheduleId: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('opens the right panel when a node is selected', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([
      { id: 'node_alpha', name: 'Node Alpha', type: 'ai', model: 'test-model', description: '测试节点' },
    ])));

    render(<AgentsSettingsPage />);
    fireEvent.click(await screen.findByRole('button', { name: /node_alpha/ }));

    await waitFor(() => expect(useSettingsSelectionStore.getState().selectedNode?.id).toBe('node_alpha'));
    expect(useSettingsStore.getState().rightPanelOpen).toBe(true);
  });

  it('opens the right panel when a tool is selected', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/admin/config/all-tool-names')) return jsonResponse(['tool_alpha']);
      return jsonResponse([
        { name: 'tool_alpha', description: '测试工具', input_schema: { type: 'object' }, timeout_sec: 30, has_spec: true },
      ]);
    }));

    render(<ToolsSettingsPage />);
    fireEvent.click(await screen.findByRole('button', { name: /tool_alpha/ }));

    await waitFor(() => expect(useSettingsSelectionStore.getState().selectedTool?.name).toBe('tool_alpha'));
    expect(useSettingsStore.getState().rightPanelOpen).toBe(true);
  });

  it('opens the right panel when a skill is selected', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([
      { name: 'skill_alpha', description: '测试技能', enabled: true, strategy: 'normal', keywords: ['alpha'] },
    ])));

    render(<SkillsSettingsPage />);
    fireEvent.click(await screen.findByRole('button', { name: /skill_alpha/ }));

    await waitFor(() => expect(useSettingsSelectionStore.getState().selectedSkill?.name).toBe('skill_alpha'));
    expect(useSettingsStore.getState().rightPanelOpen).toBe(true);
  });

  it('opens the right panel when an MCP client is selected', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([
      { id: 'mcp_alpha', enabled: true, transport: 'streamable_http', url: 'https://mcp.example.test' },
    ])));

    render(<McpSettingsPage />);
    fireEvent.click(await screen.findByRole('button', { name: /mcp_alpha/ }));

    await waitFor(() => expect(useSettingsSelectionStore.getState().selectedMcpClient?.id).toBe('mcp_alpha'));
    expect(useSettingsStore.getState().rightPanelOpen).toBe(true);
  });

  it('opens the right panel when an automation task is selected', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => settingsYamlResponse([
      'schedules:',
      '- id: schedule_alpha',
      '  cron: 0 0 * * *',
      '  type: message',
      '  text: 测试任务',
      '  enabled: true',
      '  once: false',
      '',
    ].join('\n'))));

    render(<AutomationSettingsPage />);
    fireEvent.click(await screen.findByRole('button', { name: /schedule_alpha/ }));

    await waitFor(() => expect(useSettingsSelectionStore.getState().selectedScheduleId).toBe('schedule_alpha'));
    expect(useSettingsStore.getState().rightPanelOpen).toBe(true);
  });
});
