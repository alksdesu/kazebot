import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); window.history.replaceState(null, '', '/web/'); });

describe('独立实例入口', () => {
  it.each(['', '/i/1000002', '/web/1'])('挂载 %s 使用自己的 API、WS、凭据和偏好，不借用根实例', async mount => {
    vi.resetModules();
    window.history.replaceState(null, '', `${mount}/web/?view=settings&tab=qq-persona`);
    localStorage.setItem('clonoth_admin_token', 'root-token');
    localStorage.setItem('clonoth_entry_node', 'root-node');
    localStorage.setItem(`clonoth_admin_token${mount}`, 'selected-token');
    localStorage.setItem(`clonoth_entry_node${mount}`, 'selected-node');
    localStorage.setItem('qq_console_aliases', JSON.stringify({ '123': '根实例备注' }));
    const client = await import('../api/supervisorClient');
    const { scopedKey } = await import('../store/storageKey');
    const { useSettingsStore } = await import('../store/settingsStore');
    const { LS_KEY_CLIENT_PREFS } = await import('../store/clientPrefsStore');
    const { featureRequest } = await import('../features/client');
    const { writeAlias } = await import('../console/aliases');
    expect(client.MOUNT).toBe(mount);
    expect(useSettingsStore.getState().adminToken).toBe('selected-token');
    expect(useSettingsStore.getState().entryNodeId).toBe('selected-node');
    expect(LS_KEY_CLIENT_PREFS).toBe(`clonoth_client_prefs${mount}`);
    for (const key of ['clonoth_conversation_titles', 'clonoth_auto_approved_ids']) expect(scopedKey(key)).toBe(`${key}${mount}`);
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ instances: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    await client.getInstances('selected-token');
    await featureRequest('/v1/community/state', { scope: 'qq_group:synthetic' });
    expect(fetchMock).toHaveBeenNthCalledWith(1, `${mount}/v1/instances`, expect.objectContaining({ headers: { Authorization: 'Bearer selected-token' } }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, `${mount}/v1/community/state?scope=qq_group%3Asynthetic`, expect.objectContaining({ headers: { Authorization: 'Bearer selected-token' } }));
    const socket = vi.fn();
    vi.stubGlobal('WebSocket', class { readyState = 0; constructor(url: string) { socket(url); } close() {} });
    client.connectGlobalWS(0, () => {});
    expect(socket).toHaveBeenCalledExactlyOnceWith(`ws://${window.location.host}${mount}/v1/ws`);
    client.disconnectGlobalWS();
    writeAlias('123', '本实例备注');
    expect(JSON.parse(localStorage.getItem(`qq_console_aliases${mount}`)!)).toEqual({ '123': '本实例备注' });
    if (mount) expect(JSON.parse(localStorage.getItem('qq_console_aliases')!)).toEqual({ '123': '根实例备注' });
    expect(client.instanceConsoleHref('/i/1000003')).toBe('/i/1000003/web/?view=settings&tab=qq-persona');
  });
});
