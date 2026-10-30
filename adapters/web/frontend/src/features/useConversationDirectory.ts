import { useCallback, useEffect, useState } from 'react';
import { errorMessage } from '../api/request';
import { BOOLEAN, NON_EMPTY_TEXT, at, formatError, shaped, shapedRows } from '../api/shape';
import { getConversations, MEMORY_OWNER, type ConversationRow, type MemoryOwner } from '../api/supervisorClient';
import { useRequestGuard } from '../hooks/useRequestGuard';
import { isConversationScope, platformForScope } from '../platforms/registry';
import { useSettingsStore } from '../store/settingsStore';
import { featureRequest } from './client';
import type { PolicyScope } from './community/types';

const POLICY_SCOPES = '社区设置的作用域列表';
const POLICY_SCOPE = { scope: NON_EMPTY_TEXT, current_account: BOOLEAN };

// 认不出平台的键并进会话清单，选中之后哪个平台的设置都对不上。
function readPolicyScopes(value: unknown): PolicyScope[] {
  return shapedRows(POLICY_SCOPES, shaped(POLICY_SCOPES, value, {}).items, POLICY_SCOPE, 'items').map((row, index) => {
    const path = at('items', index);
    if (!isConversationScope(row.scope)) throw formatError(POLICY_SCOPES, row.scope, at(path, 'scope'), '不是认得的会话键');
    const owner = row.owner === null ? null : shaped(POLICY_SCOPES, row.owner, MEMORY_OWNER, at(path, 'owner')) as unknown as MemoryOwner;
    return { scope: row.scope, current_account: row.current_account, owner };
  });
}

export function useConversationDirectory(includePolicies = false) {
  const token = useSettingsStore(state => state.adminToken);
  const requests = useRequestGuard(includePolicies ? 'policy-directory' : 'conversation-directory');
  const [snapshot, setSnapshot] = useState<{ key: string; rows: ConversationRow[]; policyOnlyScopes: string[]; error: string; loading: boolean }>({ key: requests.key, rows: [], policyOnlyScopes: [], error: '', loading: false });
  const reload = useCallback(async () => {
    const request = requests.begin('directory');
    setSnapshot({ key: requests.key, rows: [], policyOnlyScopes: [], error: '', loading: Boolean(token) });
    if (!token) return;
    try {
      const [result, policies] = await Promise.all([
        getConversations(token),
        includePolicies ? featureRequest('/v1/community/settings/scopes', { what: POLICY_SCOPES, signal: request.signal }) : Promise.resolve(null),
      ]);
      if (!request.isCurrent()) return;
      let rows = result;
      const policyOnlyScopes: string[] = [];
      if (includePolicies) {
        const merged = new Map(rows.map(row => [row.conversation_key, row]));
        for (const policy of readPolicyScopes(policies)) {
          const existing = merged.get(policy.scope);
          if (existing) {
            const owner = existing.owner?.name_available === true ? existing.owner : policy.owner?.name_available === true ? policy.owner : existing.owner || policy.owner;
            merged.set(policy.scope, { ...existing, owner, current_account: existing.current_account ?? policy.current_account });
          } else {
            policyOnlyScopes.push(policy.scope);
            merged.set(policy.scope, { conversation_key: policy.scope, session_id: '', channel: platformForScope(policy.scope)?.id || '', bytes: 0, updated_at: 0, owner: policy.owner, current_account: policy.current_account });
          }
        }
        rows = [...merged.values()];
      }
      setSnapshot({ key: requests.key, rows, policyOnlyScopes, error: '', loading: false });
    } catch (caught) {
      // 只说读不了和原因：读不了之后还能做什么，各处不一样，由用它的地方接着说。
      if (request.isCurrent()) setSnapshot({ key: requests.key, rows: [], policyOnlyScopes: [], error: `会话列表读取失败：${errorMessage(caught)}`, loading: false });
    }
  }, [requests, token, includePolicies]);
  useEffect(() => { void reload(); }, [reload]);
  const current = snapshot.key === requests.key;
  return { rows: current ? snapshot.rows : [], policyOnlyScopes: current ? snapshot.policyOnlyScopes : [], error: current ? snapshot.error : '', loading: current ? snapshot.loading : Boolean(token), reload };
}
