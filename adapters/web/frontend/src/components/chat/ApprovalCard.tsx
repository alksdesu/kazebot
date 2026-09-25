// [2026-05-16] Approval card — approve/deny pending operations.
import { useEffect, useRef, useState } from 'react';

import { decideApproval } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import type { ApprovalInfo } from '../../types';
import { Button, Icon } from '../common';

interface ApprovalCardProps {
  approval: ApprovalInfo;
}

export const ApprovalCard = ({ approval }: ApprovalCardProps) => {
  const [status, setStatus] = useState(approval.status);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef(false);
  const identity = useRef(approval.id);
  identity.current = approval.id;
  // Decisions also land from QQ, the settings page, or another tab, so local state alone stays 'pending' forever.
  useEffect(() => { setStatus(approval.status); setError(''); setLoading(false); pending.current = false; }, [approval.id, approval.status]);
  const adminToken = useSettingsStore(state => state.adminToken);

  const handleDecision = async (decision: 'allow' | 'deny') => {
    if (pending.current || status !== 'pending') return;
    pending.current = true;
    setLoading(true);
    setError('');
    try {
      await decideApproval(adminToken || '', approval.id, decision, `${decision} via web`);
      if (identity.current === approval.id) setStatus(decision === 'allow' ? 'allowed' : 'denied');
    } catch (failure) {
      if (identity.current === approval.id) setError(failure instanceof Error ? failure.message : '提交审批失败，请重试');
    }
    if (identity.current === approval.id) { pending.current = false; setLoading(false); }
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
      {error && <p role="alert" className="mb-2 text-xs text-[var(--duties-danger)]">{error}</p>}
      {isPending ? (
        // 这张卡只在审批找不到对应工具卡时才出现，自动审批按工具名匹配，这里注定匹配不上。
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
