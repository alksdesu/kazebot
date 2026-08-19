// [2026-06-01] Session configuration modal tests.
// Why: header node/model clicks now edit the active session in an overlay while the
// chat right rail remains the system dashboard. How: render Header and inspect the
// modal content plus viewStore state. Purpose: avoid reintroducing right-panel routing.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Header } from '../components/layout';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('SessionConfigModal from Header', () => {
  beforeEach(() => {
    localStorage.clear();
    useViewStore.setState({ viewMode: 'chat', activeSettingsTab: 'general' });
    useSettingsStore.setState({
      adminToken: 'test-token',
      isAuthenticated: true,
      isConnected: true,
      entryNodeId: 'ereuna_main',
      availableNodes: [{ id: 'ereuna_main', type: 'ai', name: 'EreunaMain', model: 'node-model' }],
      modelConfig: null,
      rightPanelOpen: true,
      activeNodeId: 'ereuna_main',
      activeNodeIsOverride: false,
      defaultNodeId: 'ereuna_main',
      globalModel: 'global-model',
      globalBaseUrl: 'https://global.example/v1',
      sessionProviderOverride: null,
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/active_node')) return jsonResponse({ node_id: 'ereuna_main', is_override: false, default_node_id: 'ereuna_main' });
      if (url.endsWith('/config')) return jsonResponse({ provider: 'openai', openai: { model: 'global-model', base_url: 'https://global.example/v1' } });
      if (url.endsWith('/admin/config/nodes')) return jsonResponse([{ id: 'ereuna_main', type: 'ai', name: 'EreunaMain', model: 'node-model' }]);
      if (url.endsWith('/provider_override')) return jsonResponse({});
      if (url.endsWith('/config/openai/secret')) return jsonResponse({ model: 'global-model', base_url: 'https://global.example/v1', api_key_present: true });
      return jsonResponse({});
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('opens a node-focused modal without changing the app view', async () => {
    render(<Header isGenerating={false} sessionId="session-1" title="Test" />);

    fireEvent.click(screen.getByTitle('切换节点'));

    expect(useViewStore.getState().viewMode).toBe('chat');
    expect(screen.getByRole('dialog', { name: '节点配置' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('活动节点')).toBeInTheDocument());
  });

  it('closes the modal from the backdrop close button', () => {
    render(<Header isGenerating={false} sessionId="session-1" title="Test" />);

    fireEvent.click(screen.getByTitle('模型配置'));
    expect(screen.getByRole('dialog', { name: '模型配置' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '关闭模型配置' }));
    expect(screen.queryByRole('dialog', { name: '模型配置' })).not.toBeInTheDocument();
  });
});
