import { useEffect, useState } from 'react';
import { errorMessage } from '../../api/request';
import { getConversations } from '../../api/supervisorClient';
import { useRequestGuard } from '../../hooks/useRequestGuard';
import { useSettingsStore } from '../../store/settingsStore';
import { startPolling } from '../../utils/polling';
import { featureRequest } from '../client';
import { actionClass } from '../ui';
import { ExecutionProgressCard } from './ExecutionProgressCard';
import { readExecutionPlans, type ExecutionPlan } from './types';

export function ConversationPlans({ sessionId }: { sessionId: string }) {
  const token = useSettingsStore(state => state.adminToken);
  const requests = useRequestGuard(sessionId);
  const [plans, setPlans] = useState<ExecutionPlan[]>([]);
  const [scope, setScope] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    let currentScope = '';
    setPlans([]); setScope(''); setError('');
    if (!sessionId || !token) return;
    async function refresh() {
      const request = requests.begin('plans');
      if (!currentScope) {
        try {
          currentScope = (await getConversations(token || '')).find(row => row.session_id === sessionId)?.conversation_key || '';
        } catch (failure) {
          // 计划是按会话键查的：会话列表没读到就不知道该查哪个会话，不是计划状态读不到。
          if (request.isCurrent()) setError(`会话列表没读到，查不出本会话有哪些计划：${errorMessage(failure)}`);
          return;
        }
        if (!currentScope) {
          if (request.isCurrent()) setError('');
          return;
        }
      }
      try {
        const data = await featureRequest<unknown>('/v1/execution/plans', { scope: currentScope, signal: request.signal });
        if (request.isCurrent()) {
          setScope(currentScope);
          setPlans(readExecutionPlans(data).filter(plan => !['cancelled', 'completed'].includes(plan.status)).slice(0, 5));
          setError('');
        }
      } catch (failure) { if (request.isCurrent()) setError(`计划状态读取失败：${errorMessage(failure)}`); }
    }
    const stop = startPolling(refresh, 5000, { first: refresh });
    const update = () => void refresh();
    window.addEventListener('clonoth:execution-updated', update);
    return () => { stop(); window.removeEventListener('clonoth:execution-updated', update); };
  }, [requests, sessionId, token]);
  if (!plans.length && !error) return null;
  return <details className="min-w-0 max-h-80 overflow-y-auto [overflow-wrap:anywhere] border-t border-[var(--duties-border)] p-3" open>
    <summary className="cursor-pointer text-sm font-semibold">本会话的计划 · {error ? plans.length ? `上次读取有 ${plans.length} 项，状态待更新` : '暂时无法读取' : `${plans.length}项待处理`}</summary>
    {error && <div className="mt-2 space-y-2"><p role="alert">{error}</p><button type="button" className={actionClass} onClick={() => window.dispatchEvent(new Event('clonoth:execution-updated'))}>重新读取计划</button></div>}
    <div className="mt-3 space-y-3">{plans.map(plan => <ExecutionProgressCard key={plan.id} planId={plan.id} scope={scope} />)}</div>
  </details>;
}
