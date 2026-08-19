// [2026-05-31] ApprovalBlockView adapts normalized approval blocks to the existing card style.
// Why: ApprovalCard still owns the approve/deny action behavior, while Step 2B changes
// where approval data comes from. How: convert ApprovalBlock into the legacy ApprovalInfo
// shape and render the existing component. Purpose: reuse the proven approval UI without
// coupling v2 message blocks to the old ChatMessage type.
import { ApprovalCard } from '../ApprovalCard';
import type { ApprovalBlock } from '../../../types/message';

type ApprovalCardApproval = Parameters<typeof ApprovalCard>[0]['approval'];

interface ApprovalBlockViewProps {
  block: ApprovalBlock;
}

function normalizeDetails(details: ApprovalBlock['details']): ApprovalCardApproval['details'] {
  return details as ApprovalCardApproval['details'];
}

export const ApprovalBlockView = ({ block }: ApprovalBlockViewProps) => {
  const approval: ApprovalCardApproval = {
    id: block.approvalId,
    operation: block.operation,
    details: normalizeDetails(block.details),
    status: block.status,
  };

  return (
    <div>
      <ApprovalCard approval={approval} />
      {(block.comment || block.decision) && (
        <div className="border-x border-b border-[var(--duties-border)] bg-[var(--duties-muted)] px-3 py-2 font-mono text-[0.66rem] text-[var(--duties-secondary)]">
          {block.decision && <span className="mr-2">决定：{block.decision}</span>}
          {block.comment && <span>备注：{block.comment}</span>}
        </div>
      )}
    </div>
  );
};
