// [2026-06-01] Client preferences tests for browser-local settings.
// Why: auto-approval, title generation, and render defaults must stay frontend-only.
// How: exercise the store helpers and the settings page controls against localStorage.
// Purpose: future backend policy changes cannot accidentally replace local build prefs.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ClientSettingsPage } from '../components/settings/pages/ClientSettingsPage';
import * as supervisorClient from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { shouldAutoApproveTool, useClientPrefsStore } from '../store/clientPrefsStore';

describe('client preferences store and page', () => {
  beforeEach(() => {
    localStorage.clear();
    useClientPrefsStore.getState().resetClientPrefs();
    useSettingsStore.setState({ adminToken: null, isAuthenticated: false, availableNodes: [], modelConfig: null });
  });

  afterEach(() => {
    localStorage.clear();
    useClientPrefsStore.getState().resetClientPrefs();
    useSettingsStore.setState({ adminToken: null, isAuthenticated: false, availableNodes: [], modelConfig: null });
    vi.restoreAllMocks();
  });

  it('uses safe defaults for known and unknown tool approval rules', () => {
    // [2026-06-01] Why: low-risk tools should be allowed locally by default while
    // write and command tools remain manual. How: test the pure resolver without a
    // component. Purpose: the approval automation path can share the same rule table.
    expect(shouldAutoApproveTool('read_file', {})).toBe(true);
    expect(shouldAutoApproveTool('search_in_files', {})).toBe(true);
    expect(shouldAutoApproveTool('list_dir', {})).toBe(true);
    expect(shouldAutoApproveTool('execute_command', {})).toBe(false);
    expect(shouldAutoApproveTool('unknown_tool', {})).toBe(false);
  });

  it('persists changed auto-approval rules and title settings in localStorage', () => {
    useClientPrefsStore.getState().setAutoApproveTool('execute_command', true);
    useClientPrefsStore.getState().setTitleGeneration('manual');

    expect(useClientPrefsStore.getState().autoApproveTools.execute_command).toBe(true);
    expect(useClientPrefsStore.getState().titleGeneration).toBe('manual');
    expect(localStorage.getItem('clonoth_client_prefs')).toContain('execute_command');
  });

  it('renders client settings controls and updates preferences from the UI', () => {
    render(<ClientSettingsPage />);

    const executeToggle = screen.getByLabelText('自动放行 execute_command');
    expect(executeToggle).not.toBeChecked();
    fireEvent.click(executeToggle);
    expect(useClientPrefsStore.getState().autoApproveTools.execute_command).toBe(true);

    const titleSelect = screen.getByLabelText('对话标题生成方式');
    expect(titleSelect).toHaveValue('first-message');
    fireEvent.change(titleSelect, { target: { value: 'manual' } });
    expect(useClientPrefsStore.getState().titleGeneration).toBe('manual');

    const thinkingToggle = screen.getByLabelText('默认折叠思考内容');
    fireEvent.click(thinkingToggle);
    expect(useClientPrefsStore.getState().thinkingDefaultCollapsed).toBe(false);
  });

  it('loads additional backend tools below the recommended approval rules', async () => {
    // [2026-06-01] Why: the UI must show every approval-capable backend tool, not
    // only the seven recommended defaults. How: mock the new API and verify an
    // extra tool appears in the separate "other tools" section with manual default.
    // Purpose: future backend tools become configurable without a frontend release.
    useSettingsStore.setState({ adminToken: 'secret-token', isAuthenticated: true });
    vi.spyOn(supervisorClient, 'getAllToolNames').mockResolvedValue(['read_file', 'execute_command', 'gemini_image']);

    render(<ClientSettingsPage />);

    expect(screen.getByText('推荐工具')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('其他工具')).toBeInTheDocument());
    expect(screen.getByText('gemini_image')).toBeInTheDocument();
    expect(screen.getByLabelText('自动放行 gemini_image')).not.toBeChecked();
  });

  it('falls back to recommended tools when backend tool loading fails', async () => {
    // [2026-06-01] Why: unauthenticated users can still edit the safe recommended
    // local rules. How: force the dynamic list request to fail and assert the
    // fallback list stays visible. Purpose: settings remain usable without admin
    // access while unknown backend tools stay manual by omission.
    useSettingsStore.setState({ adminToken: 'bad-token', isAuthenticated: true });
    vi.spyOn(supervisorClient, 'getAllToolNames').mockRejectedValue(new Error('401'));

    render(<ClientSettingsPage />);

    expect(screen.getByText('推荐工具')).toBeInTheDocument();
    await waitFor(() => expect(supervisorClient.getAllToolNames).toHaveBeenCalledWith('bad-token'));
    expect(screen.queryByText('其他工具')).not.toBeInTheDocument();
    expect(screen.getByText('read_file')).toBeInTheDocument();
  });
});
