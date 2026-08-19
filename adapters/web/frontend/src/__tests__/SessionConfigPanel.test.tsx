// [2026-06-01] SessionConfigPanel UX regression tests for the compact session header.
// Why: the panel already uses real Supervisor data, so this change is about making
// inherited data and connection state easier to read. How: mock only the transport
// endpoints and assert the rendered fallback/source labels. Purpose: future edits
// cannot reintroduce noisy WebSocket text or hide where model values come from.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SessionConfigPanel } from '../components/settings/SessionConfigPanel';
import { useChatStore, type ConnectionStatus } from '../store/chatStore';
import { useSettingsStore } from '../store/settingsStore';
import type { NodeDef } from '../types';

type TestNodeDef = NodeDef & { base_url?: string };

const activeNode: TestNodeDef = {
  id: 'ereuna_main',
  type: 'ai',
  name: 'EreunaMain',
  model: 'node-model',
  base_url: 'https://node.example/v1',
};

function responseJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function seedStores(override: Record<string, unknown> | null, connectionStatus: ConnectionStatus) {
  localStorage.clear();
  useSettingsStore.setState({
    adminToken: 'test-token',
    isAuthenticated: true,
    isConnected: true,
    entryNodeId: 'ereuna_main',
    availableNodes: [activeNode],
    modelConfig: null,
    activeNodeId: 'ereuna_main',
    activeNodeIsOverride: false,
    defaultNodeId: 'ereuna_main',
    globalModel: 'global-model',
    globalBaseUrl: 'https://global.example/v1',
    sessionProviderOverride: override,
    rightPanelOpen: true,
  });
  useChatStore.setState({ connectionStatus });
}

function stubSupervisorFetch(override: Record<string, unknown> = {}) {
  // [2026-06-01] Keep these tests tied to the real client contract, not component
  // internals. Why: SessionConfigPanel loads active node, global config, and session
  // override through fetch-based API helpers. How: route each endpoint to a stable
  // JSON payload. Purpose: the test verifies real fallback behavior without a live
  // Supervisor server.
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/active_node')) {
      return responseJson({ node_id: 'ereuna_main', is_override: false, default_node_id: 'ereuna_main' });
    }
    if (url.endsWith('/provider_override')) {
      return responseJson(override);
    }
    if (url.endsWith('/config/openai/secret')) {
      return responseJson({ model: 'global-model', base_url: 'https://global.example/v1', api_key_present: false });
    }
    if (url.endsWith('/config')) {
      return responseJson({ provider: 'openai', openai: { model: 'global-model', base_url: 'https://global.example/v1' } });
    }
    return responseJson({});
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  useChatStore.getState().resetState();
});

describe('SessionConfigPanel compact session UX', () => {
  it('collapses session details by default and removes verbose WebSocket text for an open connection', () => {
    seedStores(null, 'open');
    stubSupervisorFetch({});

    render(<SessionConfigPanel sessionId="a9ab8c5a-1111-2222" />);

    const summary = screen.getByText(/会话：a9ab8c5a/);
    const details = summary.closest('details') as HTMLDetailsElement;
    expect(details).not.toHaveAttribute('open');
    expect(screen.queryByText(/WebSocket/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Connected/i)).not.toBeInTheDocument();

    fireEvent.click(summary);

    expect(details.open).toBe(true);
    expect(screen.getByText('完整会话 ID')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /a9ab8c5a-1111-2222/ })).toBeInTheDocument();
  });

  it('shows a short abnormal connection label without using idle or open wording', () => {
    seedStores(null, 'reconnecting');
    stubSupervisorFetch({});

    render(<SessionConfigPanel sessionId="a9ab8c5a-1111-2222" />);

    expect(screen.getByText('重连中')).toBeInTheDocument();
    expect(screen.queryByText(/WebSocket/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/idle/i)).not.toBeInTheDocument();
  });

  it('keeps idle visually quiet instead of showing a disconnected warning', () => {
    seedStores(null, 'idle');
    stubSupervisorFetch({});

    render(<SessionConfigPanel sessionId="a9ab8c5a-1111-2222" />);

    // [2026-06-01] Idle means no active realtime socket is needed after normal
    // cleanup. Why: rendering idle as red made successful turns look broken. How:
    // assert that idle has no text warning and uses a muted dot. Purpose: the panel
    // only calls out unexpected connection failures.
    expect(screen.queryByText('已断开')).not.toBeInTheDocument();
    expect(screen.queryByText(/idle/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText('空闲')).toHaveClass('bg-[var(--duties-tertiary)]', 'opacity-40');
  });

  it('marks inherited model and base URL values with their node source', () => {
    seedStores(null, 'open');
    stubSupervisorFetch({});

    render(<SessionConfigPanel sessionId="session-1" />);

    expect(screen.getByText('node-model')).toBeInTheDocument();
    expect(screen.getByText('https://node.example/v1')).toBeInTheDocument();
    expect(screen.getAllByText('（节点：ereuna_main）')).toHaveLength(2);
  });

  it('marks model and base URL values that come from a session override', async () => {
    const override = { model: 'session-model', base_url: 'https://session.example/v1' };
    seedStores(override, 'open');
    stubSupervisorFetch(override);

    render(<SessionConfigPanel sessionId="session-1" />);

    await waitFor(() => expect(screen.getByText('session-model')).toBeInTheDocument());
    expect(screen.getByText('https://session.example/v1')).toBeInTheDocument();
    expect(screen.getAllByText('（会话覆盖）')).toHaveLength(2);
  });
});
