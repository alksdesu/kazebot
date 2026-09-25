import { useEffect, useState } from 'react';
import { getConversations } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { featureRequest } from '../client';
import { actionClass } from '../ui';
import { ExecutionProgressCard } from './ExecutionProgressCard';
import { readExecutionPlans, type ExecutionPlan } from './types';

export function ConversationPlans({ sessionId }: { sessionId: string }) {
  const token = useSettingsStore(state => state.adminToken);
  const [plans, setPlans] = useState<ExecutionPlan[]>([]);
  const [scope, setScope] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    let currentScope = '';
    let generation = 0;
    setPlans([]); setScope(''); setError('');
    if (!sessionId || !token) return;
    async function refresh() {
      const request = ++generation;
      try {
        if (!currentScope) currentScope = (await getConversations(token || '')).find(row => row.session_id === sessionId)?.conversation_key || '';
        if (!currentScope) return;
        const data = await featureRequest<unknown>('/v1/execution/plans', { scope: currentScope });
        if (alive && request === generation) {
          setScope(currentScope);
          setPlans(readExecutionPlans(data).filter(plan => !['cancelled', 'completed'].includes(plan.status)).slice(0, 5));
          setError('');
        }
      } catch (failure) { if (alive && request === generation) setError(String(failure)); }
    }
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    const update = () => void refresh();
    window.addEventListener('clonoth:execution-updated', update);
    return () => { alive = false; clearInterval(timer); window.removeEventListener('clonoth:execution-updated', update); };
  }, [sessionId, token]);
  if (!plans.length && !error) return null;
  return <details className="min-w-0 max-h-80 overflow-y-auto [overflow-wrap:anywhere] border-t border-[var(--duties-border)] p-3" open>
    <summary className="cursor-pointer text-sm font-semibold">本会话的计划 · {error ? plans.length ? `上次读取有 ${plans.length} 项，状态待更新` : '暂时无法读取' : `${plans.length}项待处理`}</summary>
    {error && <div className="mt-2 space-y-2"><p role="alert">计划状态读取失败，请检查连接后重试。</p><button type="button" className={actionClass} onClick={() => window.dispatchEvent(new Event('clonoth:execution-updated'))}>重新读取计划</button></div>}
    <div className="mt-3 space-y-3">{plans.map(plan => <ExecutionProgressCard key={plan.id} planId={plan.id} scope={scope} />)}</div>
  </details>;
}
