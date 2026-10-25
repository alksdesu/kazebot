// 审批决定只从这里提交：同一条审批同时只发一次，服务端回执的状态和本次决定对得上才算生效。
import { useCallback, useEffect, useRef, useState } from 'react';

import { decideApproval, type ApprovalDecision } from '../api/supervisorClient';
import { errorMessage } from '../api/request';
import { tokenChanged, useSettingsStore } from '../store/settingsStore';

export type { ApprovalDecision } from '../api/supervisorClient';

const APPROVAL_STATUS_LABELS: Readonly<Record<string, string>> = {
  pending: '待审批',
  allowed: '已允许',
  denied: '已拒绝',
};

const STATUS_AFTER: Readonly<Record<ApprovalDecision, string>> = {
  allow: 'allowed',
  deny: 'denied',
};

export interface ApprovalReceipt {
  /** false：这条审批在本次提交之前已被别处处理，status 是它现在的状态。 */
  applied: boolean;
  status: string;
}

export interface ApprovalOutcome extends ApprovalReceipt {
  failed: boolean;
  /** 给人看的那句：没生效时说明它现在的状态，失败时是原因，生效时为空串。 */
  message: string;
}

export const approvalStatusLabel = (status: string): string => APPROVAL_STATUS_LABELS[status] || status || '未知';

export const staleApprovalMessage = (status: string): string => `该审批已被处理，当前状态：${approvalStatusLabel(status)}`;

// 按审批 id 合并并发提交，共享的是服务端回的状态；各调用方再拿自己的决定去比。
const inflight = new Map<string, Promise<string>>();

export async function submitApprovalDecision(
  approvalId: string,
  decision: ApprovalDecision,
  comment: string,
  token?: string,
): Promise<ApprovalReceipt> {
  let pending = inflight.get(approvalId);
  if (!pending) {
    pending = decideApproval(token, approvalId, decision, comment)
      .then((approval) => (typeof approval?.status === 'string' ? approval.status : ''))
      .finally(() => { inflight.delete(approvalId); });
    inflight.set(approvalId, pending);
  }
  const status = await pending;
  return { applied: status === STATUS_AFTER[decision], status };
}

export interface ApprovalDecisionControls {
  /** 返回 null：这条已在提交中，或者结果回来时已换人登录、组件已卸载。 */
  decide: (approvalId: string, decision: ApprovalDecision, comment?: string) => Promise<ApprovalOutcome | null>;
  isBusy: (approvalId: string) => boolean;
  /** 最近一次提交留下的那句，没生效和失败都在这里。 */
  messageOf: (approvalId: string) => string;
}

export function useApprovalDecision(): ApprovalDecisionControls {
  const pending = useRef(new Set<string>());
  const mounted = useRef(true);
  const [busy, setBusy] = useState<ReadonlySet<string>>(() => new Set());
  const [messages, setMessages] = useState<Readonly<Record<string, string>>>({});

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const remember = useCallback((approvalId: string, text: string) => {
    setMessages((current) => ((current[approvalId] || '') === text ? current : { ...current, [approvalId]: text }));
  }, []);

  const decide = useCallback(async (approvalId: string, decision: ApprovalDecision, comment = '') => {
    if (pending.current.has(approvalId)) return null;
    const token = useSettingsStore.getState().adminToken;
    const current = () => mounted.current && !tokenChanged(token);
    pending.current.add(approvalId);
    setBusy(new Set(pending.current));
    remember(approvalId, '');
    let outcome: ApprovalOutcome;
    try {
      const receipt = await submitApprovalDecision(approvalId, decision, comment, token || undefined);
      outcome = { ...receipt, failed: false, message: receipt.applied ? '' : staleApprovalMessage(receipt.status) };
    } catch (failure) {
      outcome = { applied: false, status: '', failed: true, message: errorMessage(failure, '提交审批失败，请重试') };
    } finally {
      pending.current.delete(approvalId);
      if (mounted.current) setBusy(new Set(pending.current));
    }
    if (!current()) return null;
    remember(approvalId, outcome.message);
    return outcome;
  }, [remember]);

  return {
    decide,
    isBusy: (approvalId) => busy.has(approvalId),
    messageOf: (approvalId) => messages[approvalId] || '',
  };
}
