// [2026-05-16] Approval card — approve/deny pending operations.
import { useState } from 'react';

import { decideApproval } from '../../api/supervisorClient';
import { shouldAutoApproveTool, useClientPrefsStore } from '../../store/clientPrefsStore';
import type { ApprovalInfo } from '../../types';
import { Button, Icon } from '../common';

interface ApprovalCardProps {
  approval: ApprovalInfo;
}

export const ApprovalCard = ({ approval }: ApprovalCardProps) => {
  const [status, setStatus] = useState(approval.status);
  const [loading, setLoading] = useState(false);
  const autoApproveTools = useClientPrefsStore(state => state.autoApproveTools);
  const isAutoApprovedPending = status === 'pending' && shouldAutoApproveTool(approval.operation, autoApproveTools);

  const handleDecision = async (decision: 'allow' | 'deny') => {
    setLoading(true);
    try {
      await decideApproval(approval.id, decision, `${decision} via web`);
      setStatus(decision === 'allow' ? 'allowed' : 'denied');
    } catch {
      setStatus('denied');
    }
    setLoading(false);
  };

  const isPending = status === 'pending';

  return (
    <div className="border border-[var(--duties-border)] bg-[var(--duties-muted)] p-3">
      <div className="mb-2 flex items-center gap-2">
        {/* [2026-06-01] Why: approval card headers used a lock emoji.
            How: render verified_user through Material Symbols. Purpose: legacy
            approval UI matches the v2 tool approval icon system. */}
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
      {isPending ? (
        isAutoApprovedPending ? (
          <div className="inline-flex items-center gap-1 rounded-sm bg-gray-100 px-2 py-1 text-xs font-semibold text-gray-600">
            {/* [2026-06-01] Why: legacy approval blocks can still be rendered for
                events without tool_call_id. How: mirror ToolCallCard's muted local
                auto-approval badge. Purpose: auto-approved requests do not show
                contradictory manual buttons in either renderer. */}
            <Icon name="check_circle" size={14} />
            <span>已自动放行</span>
          </div>
        ) : (
          <div className="flex gap-2">
            <Button disabled={loading} onClick={() => handleDecision('allow')} variant="primary">
              {/* [2026-06-01] Why: approval action buttons used emoji marks.
                  How: render check_circle and cancel as Material Symbols. Purpose:
                  pending approval controls no longer emit emoji. */}
              <Icon name="check_circle" size={14} />
              <span>允许</span>
            </Button>
            <Button disabled={loading} onClick={() => handleDecision('deny')} variant="ghost">
              <Icon name="cancel" size={14} />
              <span>拒绝</span>
            </Button>
          </div>
        )
      ) : (
        <div className={`inline-flex items-center gap-1 text-xs font-semibold ${status === 'allowed' ? 'text-green-600' : 'text-red-500'}`}>
          {/* [2026-06-01] Why: approval result text embedded emoji.
              How: select a Material Symbol by approval status. Purpose: completed
              approval states stay inside the same icon font system. */}
          <Icon name={status === 'allowed' ? 'check_circle' : 'cancel'} size={14} />
          <span>{status === 'allowed' ? '已批准' : '已拒绝'}</span>
        </div>
      )}
    </div>
  );
};
