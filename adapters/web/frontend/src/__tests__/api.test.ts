// [2026-05-16] Updated: tests for real Supervisor API client signatures.
import { afterEach, describe, expect, it, vi } from 'vitest';

import { postInbound, connectGlobalWS, checkHealth, getAdminState, checkAdminAuth, decideApproval, getAllToolNames, fetchActiveTasks, cancelTask } from '../api/supervisorClient';

describe('Supervisor API client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('exports postInbound as a function', () => {
    expect(typeof postInbound).toBe('function');
  });

  it('exports connectGlobalWS as a function', () => {
    // [2026-06-03] Why: event transport moved from session polling to one global
    // WebSocket. How: assert the public helper exists instead of the removed polling
    // helper. Purpose: API exports match the long-lived realtime architecture.
    expect(typeof connectGlobalWS).toBe('function');
  });

  it('exports checkHealth as a function', () => {
    expect(typeof checkHealth).toBe('function');
  });

  it('exports getAdminState as a function', () => {
    expect(typeof getAdminState).toBe('function');
  });

  it('exports checkAdminAuth as a function', () => {
    expect(typeof checkAdminAuth).toBe('function');
  });

  it('exports decideApproval as a function', () => {
    expect(typeof decideApproval).toBe('function');
  });

  it('exports getAllToolNames as a function', () => {
    expect(typeof getAllToolNames).toBe('function');
  });

  it('exports fetchActiveTasks as a function', () => {
    // [AutoC 2026-06-04] Why: the System dashboard task count now opens a detail
    // modal. How: assert the API wrapper exists before implementing the client.
    // Purpose: prevent future refactors from removing the task-monitor entry point.
    expect(typeof fetchActiveTasks).toBe('function');
  });

  it('exports cancelTask as a function', () => {
    // [AutoC 2026-06-04] Why: each active-task row now owns a single-task cancel
    // button. How: assert the client wrapper exists before wiring the modal. Purpose:
    // UI code can call one typed helper instead of constructing endpoint strings.
    expect(typeof cancelTask).toBe('function');
  });

  it('fetches active task summaries with the admin bearer token', async () => {
    // [AutoC 2026-06-04] Why: active task details are served by a protected admin
    // endpoint. How: verify the wrapper calls the exact URL with the bearer token.
    // Purpose: the modal can load summaries without duplicating request plumbing.
    const fetchMock = vi.fn(async () => new Response(JSON.stringify([]), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(fetchActiveTasks('secret-token')).resolves.toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/tasks/active', {
      headers: { Authorization: 'Bearer secret-token' },
    });
  });

  it('cancels a single task with admin context headers', async () => {
    // [AutoC 2026-06-04] Why: the cancel endpoint is public today, but the System
    // modal should still send the configured admin context. How: verify both the
    // normal bearer header and the explicit X-Admin-Token header. Purpose: future
    // backend hardening can accept the same client call without changing the UI.
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(cancelTask('secret-token', 'task-abc')).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith('/v1/tasks/task-abc/cancel', {
      method: 'POST',
      headers: { Authorization: 'Bearer secret-token', 'X-Admin-Token': 'secret-token' },
    });
  });

  it('fetches all approval tool names with the admin bearer token', async () => {
    // [2026-06-01] Why: approval settings now load the backend's complete tool
    // list instead of relying only on a hard-coded frontend subset. How: assert the
    // exact admin endpoint and bearer header used by the API wrapper. Purpose: the
    // settings page can stay data-driven while DEFAULT_AUTO_APPROVE_TOOLS remains a
    // local-storage default only.
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(['read_file', 'gemini_image']), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(getAllToolNames('secret-token')).resolves.toEqual(['read_file', 'gemini_image']);
    expect(fetchMock).toHaveBeenCalledWith('/v1/admin/config/all-tool-names', {
      headers: { Authorization: 'Bearer secret-token' },
    });
  });
});
