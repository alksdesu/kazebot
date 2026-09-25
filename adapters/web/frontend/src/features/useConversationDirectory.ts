import { useCallback, useEffect, useRef, useState } from 'react';
import { getConversations, type ConversationRow } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { useActorRequestScope } from './useActorRequestScope';
import { featureRequest } from './client';
import type { PolicyScope } from './community/types';

export function useConversationDirectory(includePolicies = false) {
  const token = useSettingsStore(state => state.adminToken);
  const identity = useActorRequestScope(includePolicies ? 'policy-directory' : 'conversation-directory');
  const sequence = useRef(0);
  const [snapshot, setSnapshot] = useState<{ key: string; rows: ConversationRow[]; policyOnlyScopes: string[]; error: string; loading: boolean }>({ key: identity.key, rows: [], policyOnlyScopes: [], error: '', loading: false });
  const reload = useCallback(async () => {
    const request = ++sequence.current;
    setSnapshot({ key: identity.key, rows: [], policyOnlyScopes: [], error: '', loading: Boolean(token) });
    if (!token) return;
    try {
      const [result, policies] = await Promise.all([
        getConversations(token),
        includePolicies ? featureRequest<{ items: PolicyScope[] }>('/v1/community/settings/scopes') : Promise.resolve(null),
      ]);
      if (request !== sequence.current || !identity.isCurrent()) return;
      if (!Array.isArray(result)) throw new Error('invalid conversations');
      let rows = result.filter(row => row && typeof row.conversation_key === 'string');
      const policyOnlyScopes: string[] = [];
      if (includePolicies) {
        if (!Array.isArray(policies?.items) || policies.items.some(row => !row || typeof row.scope !== 'string' || !/^qq_(group|private):.+/.test(row.scope) || typeof row.current_account !== 'boolean')) throw new Error('invalid policy scopes');
        const merged = new Map(rows.map(row => [row.conversation_key, row]));
        for (const policy of policies.items) {
          const existing = merged.get(policy.scope);
          if (existing) {
            const owner = existing.owner?.name_available === true ? existing.owner : policy.owner?.name_available === true ? policy.owner : existing.owner || policy.owner;
            merged.set(policy.scope, { ...existing, owner, current_account: existing.current_account ?? policy.current_account });
          } else {
            policyOnlyScopes.push(policy.scope);
            merged.set(policy.scope, { conversation_key: policy.scope, session_id: '', channel: 'qq', bytes: 0, updated_at: 0, owner: policy.owner, current_account: policy.current_account });
          }
        }
        rows = [...merged.values()];
      }
      setSnapshot({ key: identity.key, rows, policyOnlyScopes, error: '', loading: false });
    } catch {
      if (request === sequence.current && identity.isCurrent()) setSnapshot({ key: identity.key, rows: [], policyOnlyScopes: [], error: '会话列表读取失败，仍可操作当前会话。', loading: false });
    }
  }, [token, identity.key, includePolicies]);
  useEffect(() => { void reload(); return () => { sequence.current++; }; }, [reload]);
  return { rows: snapshot.key === identity.key ? snapshot.rows : [], policyOnlyScopes: snapshot.key === identity.key ? snapshot.policyOnlyScopes : [], error: snapshot.key === identity.key ? snapshot.error : '', loading: snapshot.key === identity.key ? snapshot.loading : Boolean(token), reload };
}
