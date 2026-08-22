// 本机偏好：自动审批、标题生成、渲染折叠都只写 localStorage，后端策略碰不到它们。
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AutoApproveSection } from '../components/settings/pages/AutoApproveSection';
import { ClientSettingsPage } from '../components/settings/pages/ClientSettingsPage';
import { useSettingsSelectionStore } from '../store/settingsSelectionStore';
import { useSettingsStore } from '../store/settingsStore';
import { shouldAutoApproveTool, useClientPrefsStore } from '../store/clientPrefsStore';

describe('client preferences store and page', () => {
  beforeEach(() => {
    localStorage.clear();
    useClientPrefsStore.getState().resetClientPrefs();
    useSettingsSelectionStore.setState({ allToolNames: [] });
    useSettingsStore.setState({ adminToken: null, isAuthenticated: false, availableNodes: [], modelConfig: null });
  });

  afterEach(() => {
    localStorage.clear();
    useClientPrefsStore.getState().resetClientPrefs();
    useSettingsSelectionStore.setState({ allToolNames: [] });
    useSettingsStore.setState({ adminToken: null, isAuthenticated: false, availableNodes: [], modelConfig: null });
    vi.restoreAllMocks();
  });

  it('uses safe defaults for known and unknown tool approval rules', () => {
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

    const titleSelect = screen.getByLabelText('对话标题生成方式');
    expect(titleSelect).toHaveValue('first-message');
    fireEvent.change(titleSelect, { target: { value: 'manual' } });
    expect(useClientPrefsStore.getState().titleGeneration).toBe('manual');

    const thinkingToggle = screen.getByLabelText('默认折叠思考内容');
    fireEvent.click(thinkingToggle);
    expect(useClientPrefsStore.getState().thinkingDefaultCollapsed).toBe(false);
  });

  it('no longer offers approval rules on the client page', () => {
    // 自动审批挪去了「工具与权限」，留在这里会让人以为它跟服务端策略是一回事。
    render(<ClientSettingsPage />);

    expect(screen.queryByLabelText('自动放行 execute_command')).not.toBeInTheDocument();
    expect(screen.queryByText('推荐工具')).not.toBeInTheDocument();
  });

  it('toggles an approval rule from the tools page section', () => {
    render(<AutoApproveSection />);

    const executeToggle = screen.getByLabelText('自动放行 execute_command');
    expect(executeToggle).not.toBeChecked();
    fireEvent.click(executeToggle);
    expect(useClientPrefsStore.getState().autoApproveTools.execute_command).toBe(true);
  });

  it('lists additional backend tools below the recommended approval rules', () => {
    // 工具名由同页的工具清单拉好放进 store，这里不再自己发一次请求。
    useSettingsSelectionStore.setState({ allToolNames: ['read_file', 'execute_command', 'gemini_image'] });

    render(<AutoApproveSection />);

    expect(screen.getByText('推荐工具')).toBeInTheDocument();
    expect(screen.getByText('其他工具')).toBeInTheDocument();
    expect(screen.getByText('gemini_image')).toBeInTheDocument();
    expect(screen.getByLabelText('自动放行 gemini_image')).not.toBeChecked();
  });

  it('falls back to recommended tools when the tool list is unavailable', () => {
    render(<AutoApproveSection />);

    expect(screen.getByText('推荐工具')).toBeInTheDocument();
    expect(screen.queryByText('其他工具')).not.toBeInTheDocument();
    expect(screen.getByText('read_file')).toBeInTheDocument();
  });
});
