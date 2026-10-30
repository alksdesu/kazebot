// 找不到对应工具卡的审批单独成卡：允许或拒绝一条待审批操作。
import { useState } from 'react';

import { useApprovalDecision, type ApprovalDecision } from '../../hooks/useApprovalDecision';
import type { ApprovalInfo } from '../../types';
import { Icon } from '../common';
import { Button } from '../settings/pages/settingsControls';

interface ApprovalCardProps {
  approval: ApprovalInfo;
}

interface SettledDecision {
  id: string;
  /** 回执落下时外面传进来的状态。外面的状态变了就以外面为准。 */
  base: ApprovalInfo['status'];
  status: 'allowed' | 'denied' | 'expired';
  expiredReason?: string;
}

export const ApprovalCard = ({ approval }: ApprovalCardProps) => {
  const { decide, isBusy, messageOf } = useApprovalDecision();
  const [settled, setSettled] = useState<SettledDecision | null>(null);
  // 决定也会从 QQ、设置页或别的标签页落下，本地只叠加自己这一次的回执。
  const ownReceipt = settled && settled.id === approval.id && settled.base === approval.status ? settled : null;
  const status = ownReceipt ? ownReceipt.status : approval.status;
  const expiredReason = ownReceipt ? ownReceipt.expiredReason : approval.expiredReason;
  const loading = isBusy(approval.id);
  const message = messageOf(approval.id);

  const handleDecision = async (decision: ApprovalDecision) => {
    if (loading || status !== 'pending') return;
    const base = approval.status;
    const outcome = await decide(approval.id, decision, `${decision} via web`);
    if (outcome?.expiredReason) {
      setSettled({ id: approval.id, base, status: 'expired', expiredReason: outcome.expiredReason });
    } else if (outcome && (outcome.status === 'allowed' || outcome.status === 'denied')) {
      setSettled({ id: approval.id, base, status: outcome.status });
    }
  };

  return (
    <div className="border border-[var(--duties-border)] bg-[var(--duties-muted)] p-3">
      <div className="mb-2 flex items-center gap-2">
        <Icon name="verified_user" size={16} />
        <span className="font-mono text-xs font-semibold">需要审批</span>
      </div>
      <div className="mb-2 space-y-1 text-xs">
        <div><span className="text-[var(--duties-tertiary)]">操作：</span> <code className="text-[var(--duties-text)]">{approval.operation}</code></div>
        {approval.details.path && (
          <div><span className="text-[var(--duties-tertiary)]">路径：</span> <code className="text-[var(--duties-text)]">{approval.details.path}</code></div>
        )}
        {approval.details.reason && (
          <div><span className="text-[var(--duties-tertiary)]">原因：</span> {approval.details.reason}</div>
        )}
      </div>
      {message && <p role="alert" className="mb-2 text-xs text-[var(--duties-danger)]">{message}</p>}
      {status === 'pending' ? (
        // 这张卡只在审批找不到对应工具卡时才出现，自动审批按工具名匹配，这里注定匹配不上。
        <div className="flex gap-2">
          <Button disabled={loading} onClick={() => void handleDecision('allow')}>
            <Icon name="check_circle" size={14} />
            <span>允许</span>
          </Button>
          <Button tone="quiet" disabled={loading} onClick={() => void handleDecision('deny')}>
            <Icon name="cancel" size={14} />
            <span>拒绝</span>
          </Button>
        </div>
      ) : status === 'expired' ? (
        <div className="inline-flex items-center gap-1 text-xs font-semibold text-[var(--duties-tertiary)]">
          <Icon name="schedule" size={14} />
          <span>{expiredReason ? `已失效：${expiredReason}` : '已失效'}</span>
        </div>
      ) : (
        <div className={`inline-flex items-center gap-1 text-xs font-semibold ${status === 'allowed' ? 'text-[var(--duties-success)]' : 'text-[var(--duties-danger)]'}`}>
          <Icon name={status === 'allowed' ? 'check_circle' : 'cancel'} size={14} />
          <span>{status === 'allowed' ? '已批准' : '已拒绝'}</span>
        </div>
      )}
    </div>
  );
};
