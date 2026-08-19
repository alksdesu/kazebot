// [2026-05-31] Pure reducer for replaying SupervisorEvent records into the new chat message model.
// Why: the frontend needs one deterministic path for user messages, assistant streams, tools,
// approvals, notices, and audit-only events. How: normalize each event into immutable ChatState
// tables and keep stable ids for turns, blocks, and tool executions. Purpose: WebSocket replay
// and reconnect catch-up can rebuild the same UI state without relying on live component state.
import type { SupervisorEvent } from '../types/chat';
import type {
  ApprovalBlock,
  Attachment,
  ChatState,
  EventLogEntry,
  MessageSource,
  MessageStatus,
  NoticeBlock,
  RenderBlock,
  TaskNodeInfo,
  TextBlock,
  ThinkingBlock,
  ToolBlock,
  ToolExecution,
  ToolStatus,
  WsMessage,
} from '../types/message';

// Why: control tools do not represent user-visible work once they succeed. How:
// keep the canonical names in one set, including ask, which also terminates a turn by
// requesting user input. Purpose: successful control calls stay out of tool blocks.
const CONTROL_TOOL_NAMES = new Set(['finish', 'reply', 'switch_node', 'ask']);
const LOG_ONLY_EVENTS = new Set(['handoff_progress', 'context_usage']);

// Why: system nodes (memory_extractor, dream, compactor, turn_summarizer) run within
// the same session but their events should not create user-visible message cards.
// How: check if the event's node_id starts with 'system.' and skip card rendering.
// Purpose: prevent internal maintenance tasks from polluting the chat UI.
const SYSTEM_NODE_PREFIX = 'system.';
const TERMINAL_TOOL_STATUSES = new Set<ToolStatus>(['async_started', 'success', 'error', 'cancelled']);
// Why: reconnect catch-up can replay very long sessions. How: bound audit rows and
// idempotency keys in the reducer itself. Purpose: the browser store mirrors backend
// retention behavior instead of growing for the lifetime of the tab.
const MAX_EVENT_LOG = 3000;
const MAX_PROCESSED_IDS = 5000;

type EventPayload = Record<string, unknown>;

type ToolPatch = {
  id?: string;
  itemId?: string;
  index?: number;
  name?: string;
  status?: ToolStatus;
  arguments?: Record<string, unknown>;
  argumentsText?: string;
  argumentsTextDelta?: string;
  summary?: string;
  result?: unknown;
  rawInline?: string;
  format?: string;
  elapsedMs?: number;
  error?: string;
  taskId?: string;
  nodeId?: string;
  nodeName?: string;
  rejected?: boolean;
  // [AutoC 2026-05-31] Why: approval state is now part of ToolExecution updates.
  // How: allow reducer patches and direct approval handlers to preserve these
  // fields across later tool_call_end events. Purpose: the ToolCallCard keeps the
  // approval decision visible after the tool produces its result.
  approvalId?: string;
  approvalStatus?: ToolExecution['approvalStatus'];
  approvalDetails?: Record<string, unknown>;
};

export function createInitialChatState(): ChatState {
  return {
    messagesById: {},
    messageOrderByConversation: {},
    toolExecutionsById: {},
    toolExecutionOrder: [],
    eventLog: [],
    processedEventIds: {},
    lastSeqBySession: {},
    conversationIdsBySession: {},
    assistantMessageByTurn: {},
    userMessageByInboundSeq: {},
    taskTurnKeys: {},
    toolStableIdByExternalId: {},
    toolStableIdByIndex: {},
    approvalBlockById: {},
    nodeByTaskId: {},
  };
}

export function reduceChatEvent(state: ChatState, event: SupervisorEvent): ChatState {
  const eventId = getEventId(event);

  if (state.processedEventIds[eventId]) {
    // Why: EventLog catch-up and WebSocket live delivery can overlap. Returning the
    // original object proves idempotency to callers that rely on structural sharing.
    return state;
  }

  // Skip card rendering for system node events (memory_extractor, dream, etc.)
  const eventNodeId = getString((event.payload || {}).node_id);
  if (eventNodeId.startsWith(SYSTEM_NODE_PREFIX)) {
    // Still stamp the event so it's tracked in processedEventIds and eventLog
    return stampEvent(state, event, eventId);
  }

  let nextState = stampEvent(state, event, eventId);

  switch (event.type) {
    case 'inbound_message':
      nextState = applyInboundMessage(nextState, event);
      break;
    case 'stream_delta':
      nextState = applyStreamDelta(nextState, event);
      break;
    case 'stream_end':
      nextState = applyStreamEnd(nextState, event);
      break;
    case 'stream_text_final':
      nextState = applyStreamTextFinal(nextState, event);
      break;
    case 'outbound_message':
      nextState = applyOutboundMessage(nextState, event);
      break;
    case 'intermediate_reply':
      nextState = applyIntermediateReply(nextState, event);
      break;
    case 'tool_call_delta':
      nextState = applyToolCallDelta(nextState, event);
      break;
    case 'tool_call_start':
      nextState = applyToolCallStart(nextState, event);
      break;
    case 'tool_call_end':
      nextState = applyToolCallEnd(nextState, event);
      break;
    case 'task_created':
      nextState = applyTaskCreated(nextState, event);
      break;
    case 'task_started':
    case 'node_started':
      nextState = applyTaskOrNodeStarted(nextState, event);
      break;
    case 'task_completed':
      nextState = applyTaskCompleted(nextState, event);
      break;
    case 'task_cancelled':
      nextState = applyTaskCancelled(nextState, event);
      break;
    case 'approval_requested':
      nextState = applyApprovalRequested(nextState, event);
      break;
    case 'approval_decided':
      nextState = applyApprovalDecided(nextState, event);
      break;
    case 'llm_retry':
      nextState = applyLlmRetry(nextState, event);
      break;
    case 'node_switch':
      nextState = applyNodeSwitch(nextState, event);
      break;
    default:
      break;
  }

  return nextState;
}

// Why: older focused tests and call sites used the SupervisorEvent wording. How:
// keep these aliases as thin wrappers over the required reducer. Purpose: avoid a
// second replay implementation while the refactor settles on final naming.
export const reduceSupervisorEvent = reduceChatEvent;

export function replaySupervisorEvents(
  events: readonly SupervisorEvent[],
  initialState: ChatState = createInitialChatState(),
): ChatState {
  return events.reduce((currentState, event) => reduceChatEvent(currentState, event), initialState);
}

function applyInboundMessage(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const conversationId = getConversationId(state, event);
  const inboundSeq = event.seq;
  const messageId = getUserMessageId(conversationId, inboundSeq);
  const text = getString(payload.text);
  const attachments = getAttachments(payload.attachments);
  const now = event.ts;
  // [AutoC 2026-06-03] Why: supervisor-injected dispatch results arrive as
  // inbound_message events but are not human-authored user input. How: derive the
  // normalized role from the backend message_type contract before creating or
  // merging the message. Purpose: live callbacks render the same way as hydrated
  // history rows.
  const role = getInboundMessageRole(payload);
  const existing = state.messagesById[messageId];
  const textBlock = createTextBlock({
    id: `${messageId}|block:text:${getEventId(event)}`,
    event,
    text,
    delivery: 'final',
    streaming: false,
  });
  const childTaskId = getString(payload.child_task_id) || getString(payload.task_id);
  const childNodeId = getString(payload.child_node_id) || getString(payload.node_id);
  const source: MessageSource = {
    inboundSeq,
    // [AutoC 2026-06-04] Why: dispatch-result inbound payloads now use explicit
    // child_* metadata plus caller_node_id and summary. How: prefer the new fields and
    // keep legacy task_id/node_id fallbacks for older event logs. Purpose: realtime
    // callback cards render from structured data without parsing localized text.
    taskId: childTaskId || undefined,
    llmRequestId: getLlmRequestId(payload) || undefined,
    childTaskId: childTaskId || undefined,
    nodeId: childNodeId || undefined,
    childNodeId: childNodeId || undefined,
    callerNodeId: getString(payload.caller_node_id) || undefined,
    summary: getString(payload.summary) || undefined,
    nodeName: getString(payload.node_name) || undefined,
    branchSessionId: getString(payload.branch_session_id) || undefined,
    parentSessionId: getString(payload.parent_session_id) || undefined,
    childSessionId: getString(payload.child_session_id) || undefined,
  };

  const message: WsMessage = existing
    ? {
        ...existing,
        role,
        status: 'completed',
        updatedAt: now,
        source: mergeSource(existing.source, source),
        blocks: existing.blocks.length > 0 ? replaceFirstTextBlock(existing.blocks, textBlock) : [textBlock],
        attachments,
        eventIds: appendUnique(existing.eventIds, getEventId(event)),
      }
    : {
        id: messageId,
        conversationId,
        sessionId: event.session_id,
        role,
        status: 'completed',
        createdAt: now,
        updatedAt: now,
        source,
        blocks: [textBlock],
        attachments,
        eventIds: [getEventId(event)],
      };

  const nextState = upsertMessage(state, message);

  return {
    ...nextState,
    conversationIdsBySession: {
      ...nextState.conversationIdsBySession,
      [event.session_id]: conversationId,
    },
    userMessageByInboundSeq: {
      ...nextState.userMessageByInboundSeq,
      [String(inboundSeq)]: messageId,
    },
  };
}

function applyStreamDelta(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const streamType = getString(payload.type);
  const content = getString(payload.content);

  if (!content) {
    return state;
  }

  // [2026-06-02] Why: reply()/ask() closes the visible content for one LLM
  // round, but the task can continue with another LLM request. How: stream deltas
  // resolve through the post-completion boundary helper before appending tokens.
  // Purpose: thinking/text after a reply starts a fresh work card instead of
  // appearing below the reply text.
  let result = getOrCreateAssistantMessageForRoundStart(state, event);

  let message = setMessageStatus(result.message, 'streaming', event);

  if (streamType === 'thinking') {
    message = appendOrMergeThinkingBlock(message, event, content);
  } else if (streamType === 'text') {
    message = appendOrMergeTextBlock(message, event, content, 'stream', true);
  } else {
    return result.state;
  }

  result = { state: upsertMessage(result.state, message), message };
  return result.state;
}

function applyStreamTextFinal(state: ChatState, event: SupervisorEvent): ChatState {
  // [stream-clean 2026-05-31] Why: JSON tool mode leaks protocol markers into
  // stream_delta text. How: when the backend emits stream_text_final with the
  // cleaned plain text, replace all delivery='stream' text blocks with a single
  // delivery='final' block containing the authoritative text. Purpose: the user
  // sees only clean content, not raw <<<TOOL_CALL>>> markers.
  const payload = getPayload(event);
  const cleanText = getString(payload.text);
  const turnKey = getTurnKey(state, event);
  const message = getAssistantMessageByTurn(state, turnKey);

  if (!message) return state;

  // Remove all stream text blocks
  const cleanedBlocks = message.blocks.filter(
    (block) => !(block.kind === 'text' && (block as TextBlock).delivery === 'stream'),
  );

  // Add the clean text as a final block if non-empty
  if (cleanText.trim()) {
    const blockId = `${message.id}|block:text:clean:${getEventId(event)}`;
    const finalBlock: TextBlock = {
      id: blockId,
      kind: 'text',
      text: cleanText,
      delivery: 'final',
      streaming: false,
      createdAt: event.ts || new Date().toISOString(),
      updatedAt: event.ts || new Date().toISOString(),
      eventIds: [getEventId(event)],
    };
    cleanedBlocks.push(finalBlock);
  }

  const updated: WsMessage = {
    ...message,
    blocks: cleanedBlocks,
    updatedAt: event.ts || message.updatedAt,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };

  return upsertMessage(state, updated);
}

function applyStreamEnd(state: ChatState, event: SupervisorEvent): ChatState {
  const turnKey = getTurnKey(state, event);
  const message = getAssistantMessageByTurn(state, turnKey);

  if (!message) {
    return state;
  }

  // Why: stream_end only closes the text stream; tool_start/tool_end events for the
  // same turn can still arrive after it. How: check for stream text blocks BEFORE
  // finalizing (which clears streaming flags), then finalize. Purpose: the card
  // remains open for later tool activity and cannot appear finished too early.
  const hadStreamText = hasStreamTextBlock(message);
  const finalized = finalizeStreamingBlocks(message, event);
  const nextStatus: MessageStatus = message.status === 'streaming' || hadStreamText ? 'running_tools' : finalized.status;
  // [2026-06-03] Mark this card's LLM round as complete. Next stream_delta
  // (new thinking/text) will trigger a card break, matching history reconstruction
  // where each assistant message with content is a separate card.
  const withRoundComplete: WsMessage = {
    ...setMessageStatus(finalized, nextStatus, event),
    roundComplete: true,
  };
  return upsertMessage(state, withRoundComplete);
}

function applyOutboundMessage(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const text = getString(payload.text);
  const attachments = getAttachments(payload.attachments);

  if (!text && attachments.length === 0) return state;

  const targetMessage = findOutboundReplacementTarget(state, event);
  let nextState = state;
  let message: WsMessage;

  if (targetMessage) {
    const eventId = getEventId(event);
    message = {
      ...targetMessage,
      updatedAt: event.ts,
      source: mergeSource(targetMessage.source, buildMessageSource(state, event)),
      eventIds: appendUnique(targetMessage.eventIds, eventId),
    };
  } else {
    const created = getOrCreateAssistantMessage(state, event, getTurnKey(state, event));
    nextState = created.state;
    message = created.message;
  }

  message = replaceAssistantTextWithOutbound(message, event, text);

  // Read action_type from backend payload (finish/reply/ask)
  const actionType = getString(payload.action_type) as WsMessage['completionType'] | '';
  message = {
    ...setMessageStatus(finalizeStreamingBlocks(message, event), 'completed', event),
    attachments: attachments.length > 0 ? attachments : message.attachments,
    roundComplete: true,
    ...(actionType && { completionType: actionType as WsMessage['completionType'] }),
  };

  // [AutoC 2026-06-04] Why: one LLM request owns exactly one streaming card, and
  // outbound_message is the backend-authoritative text for that same request. How:
  // resolve the existing assistant card by task/turn or by the latest matching
  // inbound-source card, then replace its text blocks in place instead of using an
  // outbound:event-id key. Purpose: final delivery no longer creates a duplicate card
  // or causes a visible card jump after the stream finishes.
  nextState = upsertMessage(nextState, message);
  return nextState;
}

function applyIntermediateReply(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const text = getString(payload.text);

  if (!text) {
    return state;
  }

  const turnKey = getTurnKey(state, event);
  let { state: nextState, message } = getOrCreateAssistantMessage(state, event, turnKey);

  // Why: intermediate_reply is a complete user-visible chunk, while stream text
  // preceding it may be a preview. How: close active stream blocks before appending
  // the intermediate block. Purpose: the UI does not show an endless streaming mark.
  message = finalizeStreamingBlocks(message, event);
  message = appendOrMergeTextBlock(message, event, text, 'intermediate', false);
  message = {
    ...setMessageStatus(message, 'running_tools', event),
    // [2026-06-02] Why: MessageCard now draws reply styling from message-level
    // completionType instead of TextBlock delivery. How: mark live intermediate
    // reply events as reply completions while preserving their intermediate text
    // delivery. Purpose: streaming reply cards keep the blue assistant border and
    // user messages cannot inherit borders from TextBlockView.
    completionType: 'reply',
  };

  nextState = upsertMessage(nextState, message);
  return nextState;
}

function applyToolCallDelta(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const deltaType = getString(payload.event);

  if (deltaType === 'tool_call_start') {
    const patch: ToolPatch = {
      id: getString(payload.id) || getString(payload.tool_call_id),
      index: getNumber(payload.index),
      name: getString(payload.name) || getString(payload.tool_name),
      status: 'args_streaming',
      taskId: getString(payload.task_id),
      nodeId: getString(payload.node_id),
      nodeName: getString(payload.node_name),
    };
    return applyToolPatchToAssistant(state, event, patch, true);
  }

  if (deltaType === 'tool_call_args_delta' || payload.delta !== undefined || payload.arguments_delta !== undefined) {
    const delta = getString(payload.delta) || getString(payload.arguments_delta);
    const patch: ToolPatch = {
      id: getString(payload.id) || getString(payload.tool_call_id),
      index: getNumber(payload.index),
      name: getString(payload.name) || getString(payload.tool_name),
      status: 'args_streaming',
      argumentsTextDelta: delta,
      taskId: getString(payload.task_id),
      nodeId: getString(payload.node_id),
      nodeName: getString(payload.node_name),
    };
    return applyToolPatchToAssistant(state, event, patch, true);
  }

  if (deltaType === 'tool_call_done') {
    const patch: ToolPatch = {
      id: getString(payload.id) || getString(payload.tool_call_id),
      index: getNumber(payload.index),
      name: getString(payload.name) || getString(payload.tool_name),
      status: 'queued',
      taskId: getString(payload.task_id),
      nodeId: getString(payload.node_id),
      nodeName: getString(payload.node_name),
    };
    return applyToolPatchToAssistant(state, event, patch, true);
  }

  return state;
}

function applyToolCallStart(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const args = getRecord(payload.arguments);
  const patch: ToolPatch = {
    id: getString(payload.tool_call_id) || getString(payload.id),
    itemId: getString(payload.item_id),
    index: getNumber(payload.index),
    name: getString(payload.tool_name) || getString(payload.name),
    status: 'running',
    arguments: args,
    argumentsText: args ? stringifyJson(args) : undefined,
    taskId: getString(payload.task_id),
    nodeId: getString(payload.node_id),
    nodeName: getString(payload.node_name),
  };

  return applyToolPatchToAssistant(state, event, patch, true);
}

function applyToolCallEnd(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const result = payload.result;
  const resultRecord = getRecord(result);
  const patch: ToolPatch = {
    id: getString(payload.tool_call_id) || getString(payload.id),
    itemId: getString(payload.item_id),
    index: getNumber(payload.index),
    name: getString(payload.tool_name) || getString(payload.name),
    status: normalizeToolStatus(payload.status),
    summary: getString(payload.summary),
    result,
    rawInline: getString(payload.raw_inline),
    format: getString(payload.format),
    elapsedMs: getNumber(payload.elapsed_ms),
    error: getString(payload.error) || (resultRecord ? getString(resultRecord.error) : ''),
    taskId: getString(payload.task_id),
    nodeId: getString(payload.node_id),
    nodeName: getString(payload.node_name),
    rejected: getBoolean(payload.rejected),
  };

  return applyToolPatchToAssistant(state, event, patch, true);
}

function applyTaskCreated(state: ChatState, event: SupervisorEvent): ChatState {
  // Why: later engine events often carry only task_id. How: bind the task to the
  // current turn as soon as the supervisor snapshot is seen. Purpose: streams and
  // tools emitted by that task merge into the same assistant message.
  return recordTaskAndNodeInfo(state, event);
}

function applyTaskOrNodeStarted(state: ChatState, event: SupervisorEvent): ChatState {
  let nextState = recordTaskAndNodeInfo(state, event);
  const turnKey = getTurnKey(nextState, event);
  const taskId = getString(getPayload(event).task_id);
  const shouldCreate = hasSourceInboundSeq(getPayload(event)) || Boolean(nextState.assistantMessageByTurn[turnKey]);

  if (!taskId || !shouldCreate) {
    return nextState;
  }

  const result = getOrCreateAssistantMessage(nextState, event, turnKey);
  nextState = upsertMessage(result.state, setMessageStatus(result.message, 'running_tools', event));
  return nextState;
}

function applyTaskCompleted(state: ChatState, event: SupervisorEvent): ChatState {
  const nextState = recordTaskAndNodeInfo(state, event);
  const turnKey = getTurnKey(nextState, event);
  const message = getAssistantMessageByTurn(nextState, turnKey);

  if (!message) {
    return nextState;
  }

  const payload = getPayload(event);
  const result = getRecord(payload.result);
  const action = result ? getString(result.action) : '';
  const rawStatus = getString(payload.status);
  const status: MessageStatus = rawStatus === 'cancelled' || action === 'cancelled'
    ? 'cancelled'
    : rawStatus === 'failed' || action === 'fail'
      ? 'failed'
      : 'completed';

  return upsertMessage(nextState, setMessageStatus(finalizeStreamingBlocks(message, event), status, event));
}

function applyTaskCancelled(state: ChatState, event: SupervisorEvent): ChatState {
  const nextState = recordTaskAndNodeInfo(state, event);
  const turnKey = getTurnKey(nextState, event);
  const message = getAssistantMessageByTurn(nextState, turnKey);

  if (!message) {
    return nextState;
  }

  return upsertMessage(nextState, setMessageStatus(finalizeStreamingBlocks(message, event), 'cancelled', event));
}

function applyApprovalRequested(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const approvalId = getString(payload.approval_id);
  const toolCallId = getString(payload.tool_call_id);

  if (!approvalId) {
    return state;
  }

  if (toolCallId) {
    const tool = findToolByCallId(state, toolCallId);
    if (tool) {
      // [AutoC 2026-05-31] Why: approval_requested now identifies the tool call
      // that is waiting. How: update that ToolExecution instead of appending an
      // ApprovalBlock. Purpose: the user sees the prompt and buttons in the same
      // card that already shows the tool name and arguments.
      const updatedTool: ToolExecution = {
        ...tool,
        status: 'awaiting_approval',
        approvalId,
        approvalStatus: 'pending',
        approvalDetails: buildApprovalDetails(payload),
        updatedAt: event.ts,
        eventIds: appendUnique(tool.eventIds, getEventId(event)),
      };
      let nextState = upsertToolRecord(state, updatedTool);
      const message = nextState.messagesById[tool.messageId];
      if (message) {
        nextState = upsertMessage(nextState, setMessageStatus(message, 'awaiting_approval', event));
      }
      return {
        ...nextState,
        approvalBlockById: {
          ...nextState.approvalBlockById,
          [approvalId]: {
            messageId: updatedTool.messageId,
            blockId: updatedTool.blockId || updatedTool.stableId,
            toolCallId,
          },
        },
      };
    }
  }

  return appendLegacyApprovalBlock(state, event, payload, approvalId);
}

function applyApprovalDecided(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const approvalId = getString(payload.approval_id);
  const location = approvalId ? state.approvalBlockById[approvalId] : undefined;

  if (!approvalId) {
    return state;
  }

  // [AutoC 2026-05-31] Why: clients can reconnect after approval_requested has
  // already been processed elsewhere, leaving no local approvalBlockById entry.
  // How: prefer the tool_call_id carried by approval_decided and only require the
  // legacy location for ApprovalBlock fallback. Purpose: tool-card decisions still
  // update during partial catch-up or mixed event ordering.
  const toolCallId = getString(payload.tool_call_id) || location?.toolCallId || '';
  if (toolCallId) {
    const tool = findToolByCallId(state, toolCallId);
    if (tool) {
      // [AutoC 2026-05-31] Why: approval decisions should close the inline
      // approval state on the same tool card. How: resolve the approval back to
      // the external tool_call_id and keep existing terminal tool results intact.
      // Purpose: replaying approval_decided updates the card instead of looking for
      // a standalone ApprovalBlock.
      const approvalStatus = normalizeApprovalStatus(payload.status, payload.decision);
      const updatedTool: ToolExecution = {
        ...tool,
        status: getToolStatusAfterApproval(tool.status, approvalStatus),
        approvalId,
        approvalStatus,
        approvalDetails: tool.approvalDetails || buildApprovalDetails(payload),
        updatedAt: event.ts,
        eventIds: appendUnique(tool.eventIds, getEventId(event)),
      };
      let nextState = upsertToolRecord(state, updatedTool);
      const message = nextState.messagesById[tool.messageId];
      if (message) {
        nextState = upsertMessage(nextState, setMessageStatus(message, 'running_tools', event));
      }
      return nextState;
    }
  }

  if (!location) {
    return state;
  }

  return updateLegacyApprovalBlock(state, event, payload, approvalId, location);
}

function applyLlmRetry(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const attempt = getNumber(payload.attempt);
  const maxRetries = getNumber(payload.max_retries);
  const delaySec = getNumber(payload.delay_sec);
  const error = getString(payload.error) || '未知错误';
  // [2026-06-01] Why: retry notices are rendered as user-visible chat notices.
  // How: preserve retry numbers and backend error text while translating the fixed
  // phrases. Purpose: operational notices no longer introduce English UI copy.
  const pieces = ['模型请求将重试。'];

  if (attempt !== undefined && maxRetries !== undefined) {
    pieces.push(`第 ${attempt} 次，共 ${maxRetries} 次。`);
  }
  if (delaySec !== undefined) {
    pieces.push(`${delaySec} 秒后重试。`);
  }
  pieces.push(`原因：${error}`);

  return appendNoticeToAssistant(state, event, {
    level: 'warning',
    title: '模型请求重试',
    text: pieces.join(' '),
    eventType: event.type,
  });
}

function applyNodeSwitch(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const target = getString(payload.target_node_id);
  const defaultNode = getString(payload.default_node_id);
  // [2026-06-01] Why: node-switch notices are rendered directly in chat. How:
  // preserve target/default identifiers and translate the surrounding message.
  // Purpose: operational notices are localized without altering event payloads.
  const text = target
    ? `已切换到节点 ${target}。`
    : `已清除节点切换${defaultNode ? `；默认节点为 ${defaultNode}。` : '。'}`;

  return appendNoticeToAssistant(state, event, {
    level: 'info',
    title: '节点切换',
    text,
    eventType: event.type,
  });
}

function appendNoticeToAssistant(
  state: ChatState,
  event: SupervisorEvent,
  notice: Pick<NoticeBlock, 'level' | 'text' | 'title' | 'eventType'>,
): ChatState {
  const turnKey = getTurnKey(state, event);
  let { state: nextState, message } = getOrCreateAssistantMessage(state, event, turnKey);
  const block: NoticeBlock = {
    id: `${message.id}|notice:${getEventId(event)}`,
    kind: 'notice',
    level: notice.level,
    text: notice.text,
    title: notice.title,
    eventType: notice.eventType,
    createdAt: event.ts,
    updatedAt: event.ts,
    eventIds: [getEventId(event)],
  };

  message = setMessageStatus({ ...message, blocks: [...message.blocks, block] }, 'running_tools', event);
  nextState = upsertMessage(nextState, message);
  return nextState;
}

function applyToolPatchToAssistant(
  state: ChatState,
  event: SupervisorEvent,
  patch: ToolPatch,
  attachBlock: boolean,
): ChatState {
  // [2026-06-03] Why: a reply/ask card closes one visible assistant round, but the
  // next streamed LLM round can begin with provider tool_call_delta events and no
  // text/thinking token, so stream_delta-only boundary detection misses it. How:
  // route new provider tool deltas through the same post-reply boundary helper while
  // keeping existing tool executions on their original card. Purpose: live streaming
  // uses the same card split points as structured history without moving same-round
  // tool end updates away from the reply card.
  let { state: nextState, message } = getOrCreateAssistantMessageForToolPatch(state, event, patch);
  const toolResult = upsertToolExecution(nextState, message, event, patch);
  nextState = toolResult.state;

  if (attachBlock && !toolResult.tool.hidden) {
    const attached = attachToolToMessage(nextState, message, toolResult.tool, event);
    nextState = attached.state;
    message = attached.message;
  } else {
    message = nextState.messagesById[message.id] || message;
  }

  message = setMessageStatus(message, 'running_tools', event);
  nextState = upsertMessage(nextState, message);

  if (toolResult.tool.status && TERMINAL_TOOL_STATUSES.has(toolResult.tool.status)) {
    return nextState;
  }

  return nextState;
}

function upsertToolExecution(
  state: ChatState,
  message: WsMessage,
  event: SupervisorEvent,
  patch: ToolPatch,
): { state: ChatState; tool: ToolExecution } {
  const identity = resolveToolStableId(state, message, event, patch);
  const current = state.toolExecutionsById[identity.stableId];
  const currentText = current?.argumentsText || '';
  const argumentsTextFromPatch = patch.argumentsText !== undefined
    ? patch.argumentsText
    : patch.argumentsTextDelta !== undefined
      ? `${currentText}${patch.argumentsTextDelta}`
      : patch.arguments !== undefined
        ? stringifyJson(patch.arguments)
        : current?.argumentsText;
  const parsedArguments = patch.arguments !== undefined
    ? patch.arguments
    : parseJsonRecord(argumentsTextFromPatch) || current?.arguments;
  const name = patch.name || current?.name || 'tool';
  const rejected = patch.rejected !== undefined ? patch.rejected : current?.rejected;
  // [2026-06-02] Why: a rejected tool result is a failed execution even when a
  // backend payload still carries status=success. How: resolve rejected before status
  // and coerce the visible lifecycle state to error. Purpose: live replay matches the
  // error semantics used by historical rejected tool results.
  const status: ToolStatus = rejected ? 'error' : patch.status || current?.status || 'queued';
  const control = current?.control || CONTROL_TOOL_NAMES.has(name);
  // [2026-06-02] Why: successful control tools are hidden as internal bookkeeping,
  // but rejected finish/reply/ask calls are user-visible failure explanations. How:
  // calculate rejected before hidden and clear any inherited hidden state when rejected.
  // Purpose: live stream replay shows rejected control calls the same way history does.
  const hidden = !rejected && ((current?.hidden || false) || (control && status === 'success'));
  const tool: ToolExecution = {
    stableId: identity.stableId,
    messageId: message.id,
    blockId: current?.blockId,
    id: patch.id || current?.id,
    itemId: patch.itemId || current?.itemId,
    index: patch.index !== undefined ? patch.index : current?.index,
    name,
    status,
    arguments: parsedArguments,
    argumentsText: argumentsTextFromPatch,
    summary: patch.summary !== undefined ? patch.summary : current?.summary,
    result: patch.result !== undefined ? patch.result : current?.result,
    rawInline: patch.rawInline !== undefined ? patch.rawInline : current?.rawInline,
    format: patch.format !== undefined ? patch.format : current?.format,
    elapsedMs: patch.elapsedMs !== undefined ? patch.elapsedMs : current?.elapsedMs,
    control,
    rejected,
    hidden,
    error: patch.error || current?.error,
    approvalId: patch.approvalId || current?.approvalId,
    approvalStatus: patch.approvalStatus || current?.approvalStatus,
    approvalDetails: patch.approvalDetails || current?.approvalDetails,
    taskId: patch.taskId || current?.taskId || message.source.taskId,
    nodeId: patch.nodeId || current?.nodeId || message.source.nodeId,
    nodeName: patch.nodeName || current?.nodeName || message.source.nodeName,
    createdAt: current?.createdAt || event.ts,
    updatedAt: event.ts,
    eventIds: appendUnique(current?.eventIds || [], getEventId(event)),
  };

  return {
    state: {
      ...state,
      toolExecutionsById: {
        ...state.toolExecutionsById,
        [identity.stableId]: tool,
      },
      toolExecutionOrder: current ? state.toolExecutionOrder : [...state.toolExecutionOrder, identity.stableId],
      toolStableIdByExternalId: identity.externalKey
        ? { ...state.toolStableIdByExternalId, [identity.externalKey]: identity.stableId }
        : state.toolStableIdByExternalId,
      toolStableIdByIndex: identity.indexKey
        ? { ...state.toolStableIdByIndex, [identity.indexKey]: identity.stableId }
        : state.toolStableIdByIndex,
    },
    tool,
  };
}

function attachToolToMessage(
  state: ChatState,
  message: WsMessage,
  tool: ToolExecution,
  event: SupervisorEvent,
): { state: ChatState; message: WsMessage; tool: ToolExecution } {
  const existingBlockIndex = message.blocks.findIndex(
    (block) => block.kind === 'tool' && block.toolIds.includes(tool.stableId),
  );
  const lastBlock = message.blocks[message.blocks.length - 1];
  const reusableLastToolBlock = existingBlockIndex < 0 && lastBlock?.kind === 'tool' ? lastBlock : undefined;
  let blockId = tool.blockId;
  let nextBlocks: RenderBlock[];

  if (existingBlockIndex >= 0) {
    nextBlocks = message.blocks.map((block, index) => {
      if (index !== existingBlockIndex || block.kind !== 'tool') {
        return block;
      }
      blockId = block.id;
      return {
        ...block,
        updatedAt: event.ts,
        eventIds: appendUnique(block.eventIds, getEventId(event)),
      };
    });
  } else if (reusableLastToolBlock) {
    blockId = reusableLastToolBlock.id;
    nextBlocks = message.blocks.map((block) => {
      if (block.id !== reusableLastToolBlock.id || block.kind !== 'tool') {
        return block;
      }
      return {
        ...block,
        toolIds: appendUnique(block.toolIds, tool.stableId),
        updatedAt: event.ts,
        eventIds: appendUnique(block.eventIds, getEventId(event)),
      };
    });
  } else {
    blockId = `${message.id}|block:tool:${getEventId(event)}`;
    const block: ToolBlock = {
      id: blockId,
      kind: 'tool',
      toolIds: [tool.stableId],
      createdAt: event.ts,
      updatedAt: event.ts,
      eventIds: [getEventId(event)],
    };
    nextBlocks = [...message.blocks, block];
  }

  const nextTool = blockId && tool.blockId !== blockId ? { ...tool, blockId } : tool;
  const nextMessage = {
    ...message,
    blocks: nextBlocks,
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
  const stateWithMessage = upsertMessage(state, nextMessage);
  const nextState = nextTool === tool
    ? stateWithMessage
    : {
        ...stateWithMessage,
        toolExecutionsById: {
          ...stateWithMessage.toolExecutionsById,
          [nextTool.stableId]: nextTool,
        },
      };

  return { state: nextState, message: nextMessage, tool: nextTool };
}

function recordTaskAndNodeInfo(state: ChatState, event: SupervisorEvent): ChatState {
  const payload = getPayload(event);
  const taskId = getString(payload.task_id);
  let nextState = state;

  if (taskId) {
    const turnKey = getTurnKey(state, event);
    nextState = {
      ...nextState,
      taskTurnKeys: {
        ...nextState.taskTurnKeys,
        [taskId]: turnKey,
      },
    };
  }

  const nodeInfo = getTaskNodeInfo(event);
  if (taskId && (nodeInfo.nodeId || nodeInfo.nodeName)) {
    nextState = {
      ...nextState,
      nodeByTaskId: {
        ...nextState.nodeByTaskId,
        [taskId]: mergeTaskNodeInfo(nextState.nodeByTaskId[taskId], nodeInfo),
      },
    };
  }

  return nextState;
}

function getOrCreateAssistantMessage(
  state: ChatState,
  event: SupervisorEvent,
  turnKey: string,
): { state: ChatState; message: WsMessage } {
  const existingId = state.assistantMessageByTurn[turnKey];
  const existing = existingId ? state.messagesById[existingId] : undefined;
  const eventId = getEventId(event);
  const source = buildMessageSource(state, event);

  if (existing) {
    const nextMessage: WsMessage = {
      ...existing,
      updatedAt: event.ts,
      source: mergeSource(existing.source, source),
      eventIds: appendUnique(existing.eventIds, eventId),
    };
    return { state: upsertMessage(state, nextMessage), message: nextMessage };
  }

  const conversationId = getConversationId(state, event);
  const messageId = getAssistantMessageId(conversationId, turnKey);
  const message: WsMessage = {
    id: messageId,
    conversationId,
    sessionId: event.session_id,
    role: 'assistant',
    status: 'pending',
    createdAt: event.ts,
    updatedAt: event.ts,
    source,
    blocks: [],
    eventIds: [eventId],
  };
  const nextState = upsertMessage({
    ...state,
    assistantMessageByTurn: {
      ...state.assistantMessageByTurn,
      [turnKey]: messageId,
    },
  }, message);

  return { state: nextState, message };
}

function getAssistantMessageByTurn(state: ChatState, turnKey: string): WsMessage | undefined {
  const messageId = state.assistantMessageByTurn[turnKey];
  return messageId ? state.messagesById[messageId] : undefined;
}

function findOutboundReplacementTarget(state: ChatState, event: SupervisorEvent): WsMessage | undefined {
  const payload = getPayload(event);
  const conversationId = getConversationId(state, event);
  const turnKey = getTurnKey(state, event);
  const directMessage = getAssistantMessageByTurn(state, turnKey);
  const llmRequestId = getLlmRequestId(payload);

  if (llmRequestId && directMessage) {
    // [AutoC 2026-06-04] Why: outbound_message now carries the exact provider
    // request id that produced the visible stream card. How: return the card bound
    // to that request key before running task/inbound fallback heuristics. Purpose:
    // final text replaces the original card and never targets another request in
    // the same task.
    return directMessage;
  }
  const sourceInboundSeq = getSourceInboundSeq(payload);
  const taskId = getString(payload.task_id);
  const nodeId = getString(payload.node_id);
  const order = state.messageOrderByConversation[conversationId] || [];

  for (let index = order.length - 1; index >= 0; index -= 1) {
    const message = state.messagesById[order[index]];
    if (!message || message.role !== 'assistant') continue;

    if (taskId && message.source.taskId !== taskId && message.source.childTaskId !== taskId) continue;
    if (!taskId && sourceInboundSeq !== undefined && message.source.inboundSeq !== sourceInboundSeq) continue;
    if (!taskId && sourceInboundSeq === undefined && directMessage && message.id !== directMessage.id) continue;
    if (nodeId && message.source.nodeId && message.source.nodeId !== nodeId && message.source.childNodeId !== nodeId) continue;

    // [AutoC 2026-06-04] Why: outbound_message can lack task_id after a task has
    // moved through more than one LLM request. How: scan the existing conversation
    // backwards and choose the newest assistant card that matches the source inbound
    // sequence, task, and node metadata. Purpose: the final backend payload replaces
    // the stream card for the current request instead of falling back to the first
    // inbound turn card.
    return message;
  }

  return directMessage;
}

function replaceAssistantTextWithOutbound(message: WsMessage, event: SupervisorEvent, text: string): WsMessage {
  const retainedBlocks = message.blocks.filter((block) => block.kind !== 'text');
  const blocks: RenderBlock[] = [...retainedBlocks];

  if (text) {
    blocks.push(createTextBlock({
      id: `${message.id}|block:text:outbound:${getEventId(event)}`,
      event,
      text,
      delivery: 'final',
      streaming: false,
    }));
  }

  // [AutoC 2026-06-04] Why: streamed text is provisional and may contain partial
  // protocol output, while outbound_message contains the backend's final response
  // text for the same LLM request. How: remove prior text blocks and append one final
  // text block, while preserving thinking, tools, approvals, and notices on the same
  // message id. Purpose: the visible card is replaced in place without losing work
  // trace blocks that belong to the request.
  return {
    ...message,
    blocks,
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

function shouldBreakCardForNewRound(message: WsMessage): boolean {
  // [2026-06-03] Why: each LLM round (thinking → text → tools) should be its own
  // card, matching hydrateStructuredHistory where each assistant message with content
  // becomes a separate card. How: break on reply/ask completions OR when the previous
  // round's stream has ended (roundComplete). Purpose: live streaming produces the
  // same card boundaries as history reconstruction.
  return message.completionType === 'reply' || message.completionType === 'ask' || message.roundComplete === true;
}

function isProviderToolStreamEvent(event: SupervisorEvent): boolean {
  // [2026-06-03] Why: durable tool_call_start/tool_call_end events can be emitted
  // after reply() while still belonging to the same assistant tool-call row, but
  // provider tool_call_delta events mark the streamed start of an LLM response. How:
  // treat only tool_call_delta as a possible post-reply round starter. Purpose: a
  // tool-only streamed next round gets its own card without splitting same-round
  // durable tool lifecycle updates.
  return event.type === 'tool_call_delta';
}

function hasExistingToolPatchOnMessage(state: ChatState, message: WsMessage, patch: ToolPatch): boolean {
  // [2026-06-03] Why: later args, start, approval, and end events for a tool already
  // rendered on a reply card must update that same tool instead of creating a new
  // card. How: check the reducer's stable external-id and index maps for keys scoped
  // to the current message id. Purpose: card-boundary detection only applies to new
  // tool executions, not existing same-round lifecycle patches.
  const externalKey = patch.id ? `${message.id}|external:${patch.id}` : '';
  const indexKey = patch.index !== undefined ? `${message.id}|index:${patch.index}` : '';
  return Boolean(
    (externalKey && state.toolStableIdByExternalId[externalKey])
    || (indexKey && state.toolStableIdByIndex[indexKey]),
  );
}

function getOrCreateAssistantMessageForToolPatch(
  state: ChatState,
  event: SupervisorEvent,
  patch: ToolPatch,
): { state: ChatState; message: WsMessage } {
  const turnKey = getTurnKey(state, event);
  const currentMessage = getAssistantMessageByTurn(state, turnKey);

  if (
    currentMessage
    && shouldBreakCardForNewRound(currentMessage)
    && isProviderToolStreamEvent(event)
    && !hasExistingToolPatchOnMessage(state, currentMessage, patch)
  ) {
    // [2026-06-03] Why: after reply()/ask(), the next LLM request may stream only
    // a tool call, so no stream_delta arrives to invoke the normal round-start path.
    // How: reuse getOrCreateAssistantMessageForRoundStart for a new provider tool
    // stream that is not already attached to the reply card. Purpose: tool-only
    // streamed rounds split exactly like text/thinking-started rounds.
    return getOrCreateAssistantMessageForRoundStart(state, event);
  }

  return getOrCreateAssistantMessage(state, event, turnKey);
}

function getOrCreateAssistantMessageForRoundStart(
  state: ChatState,
  event: SupervisorEvent,
): { state: ChatState; message: WsMessage } {
  const turnKey = getTurnKey(state, event);
  const currentMessage = getAssistantMessageByTurn(state, turnKey);

  if (!currentMessage || !shouldBreakCardForNewRound(currentMessage)) {
    return getOrCreateAssistantMessage(state, event, turnKey);
  }

  const completionType = currentMessage.completionType || 'reply';
  const freshTurnKey = `after-${completionType}:${turnKey}:${currentMessage.id}`;
  const payload = getPayload(event);
  const taskId = getString(payload.task_id);
  // [2026-06-02] Why: the first stream_delta after a reply is the real
  // boundary between LLM rounds, not intermediate_reply or tool lifecycle events.
  // How: close the current reply/ask card, then bind the task to a deterministic
  // key derived from the completed card id rather than from the triggering event
  // id. Purpose: every later event for the same task resolves to one new work
  // card, while same-round tools still remain on the reply card.
  const closedMessage = setMessageStatus(finalizeStreamingBlocks(currentMessage, event), 'completed', event);
  const stateWithClosedMessage = upsertMessage(state, closedMessage);
  const stateWithFreshTurn = taskId
    ? {
        ...stateWithClosedMessage,
        taskTurnKeys: {
          ...stateWithClosedMessage.taskTurnKeys,
          [taskId]: freshTurnKey,
        },
      }
    : stateWithClosedMessage;

  return getOrCreateAssistantMessage(stateWithFreshTurn, event, freshTurnKey);
}

function upsertToolRecord(state: ChatState, tool: ToolExecution): ChatState {
  // [AutoC 2026-05-31] Why: approval events update an already-created tool without
  // changing its arguments or block membership. How: replace only the normalized
  // tool table entry. Purpose: avoid manufacturing a second tool patch event just
  // to reflect approval status.
  return {
    ...state,
    toolExecutionsById: {
      ...state.toolExecutionsById,
      [tool.stableId]: tool,
    },
  };
}

function upsertMessage(state: ChatState, message: WsMessage): ChatState {
  const exists = Boolean(state.messagesById[message.id]);
  const currentOrder = state.messageOrderByConversation[message.conversationId] || [];
  const nextOrder = exists || currentOrder.includes(message.id) ? currentOrder : [...currentOrder, message.id];

  return {
    ...state,
    messagesById: {
      ...state.messagesById,
      [message.id]: message,
    },
    messageOrderByConversation: {
      ...state.messageOrderByConversation,
      [message.conversationId]: nextOrder,
    },
  };
}

function stampEvent(state: ChatState, event: SupervisorEvent, eventId: string): ChatState {
  const previousSeq = state.lastSeqBySession[event.session_id] || 0;
  const eventLogEntry = buildEventLogEntry(state, event, eventId);

  let nextLog = [...state.eventLog, eventLogEntry];
  if (nextLog.length > MAX_EVENT_LOG) {
    // Why: old event-log rows are useful for inspection but not for rendering current
    // chat state. How: retain the newest rows only, matching the backend log window.
    // Purpose: long sessions do not keep unbounded audit data in browser memory.
    nextLog = nextLog.slice(-MAX_EVENT_LOG);
  }

  let nextProcessedIds: Record<string, true> = {
    ...state.processedEventIds,
    [eventId]: true,
  };
  const processedKeys = Object.keys(nextProcessedIds);
  if (processedKeys.length > MAX_PROCESSED_IDS) {
    // Why: idempotency keys protect against recent WebSocket/EventLog overlap, but
    // keeping every key forever is unnecessary. How: keep the newest half by object
    // insertion order after the cap is exceeded. Purpose: bound memory while retaining
    // the keys most likely to be redelivered during reconnect.
    const keep = processedKeys.slice(Math.floor(processedKeys.length / 2));
    nextProcessedIds = {};
    for (const key of keep) nextProcessedIds[key] = true;
  }

  return {
    ...state,
    eventLog: nextLog,
    processedEventIds: nextProcessedIds,
    lastSeqBySession: {
      ...state.lastSeqBySession,
      [event.session_id]: Math.max(previousSeq, event.seq),
    },
  };
}

function buildEventLogEntry(state: ChatState, event: SupervisorEvent, eventId: string): EventLogEntry {
  const payload = getPayload(event);
  const turnKey = getTurnKey(state, event);
  const messageId = event.type === 'inbound_message'
    ? getUserMessageId(getConversationId(state, event), event.seq)
    : state.assistantMessageByTurn[turnKey];

  return {
    id: `log:${eventId}`,
    eventId,
    seq: event.seq,
    ts: event.ts,
    sessionId: event.session_id,
    conversationId: getConversationId(state, event),
    type: event.type,
    component: event.component,
    messageId,
    turnKey,
    payload,
    summary: summarizeEvent(event),
    hiddenFromChat: isLogOnlyEvent(event.type),
  };
}

function getTurnKey(state: ChatState, event: SupervisorEvent): string {
  const payload = getPayload(event);
  const llmRequestId = getLlmRequestId(payload);
  if (llmRequestId) {
    // [AutoC 2026-06-04] Why: a task can contain multiple LLM requests, each with its
    // own stream card. How: prefer the backend request id over task and inbound keys.
    // Purpose: stream_delta, tool events, stream_end, and outbound_message for one
    // provider request all update exactly one card.
    return `llm-request:${llmRequestId}`;
  }

  const taskId = getString(payload.task_id);
  if (taskId && state.taskTurnKeys[taskId]) {
    // [2026-06-02] Why: one backend task can span multiple visible LLM-round cards
    // after reply()/ask(). How: prefer an explicit task-to-turn binding when it is
    // present, and let source_inbound_seq seed only the initial task card below.
    // Purpose: once the reducer moves a task to a post-reply turn key, later events
    // with the same source inbound sequence do not collapse back into the reply card.
    return state.taskTurnKeys[taskId];
  }

  const sourceInboundSeq = getSourceInboundSeq(payload);

  if (sourceInboundSeq !== undefined) {
    return `inbound:${sourceInboundSeq}`;
  }

  if (taskId) {
    return `task:${taskId}`;
  }

  return `event:${getEventId(event)}`;
}

function getConversationId(state: ChatState, event: SupervisorEvent): string {
  const payload = getPayload(event);
  const explicitConversation = normalizeConversationKey(getString(payload.conversation_key));

  if (explicitConversation) {
    return explicitConversation;
  }

  const sourceInboundSeq = getSourceInboundSeq(payload);
  if (sourceInboundSeq !== undefined) {
    const userMessageId = state.userMessageByInboundSeq[String(sourceInboundSeq)];
    const userMessage = userMessageId ? state.messagesById[userMessageId] : undefined;
    if (userMessage) {
      return userMessage.conversationId;
    }
  }

  const parentSessionId = getString(payload.parent_session_id);
  if (parentSessionId && state.conversationIdsBySession[parentSessionId]) {
    return state.conversationIdsBySession[parentSessionId];
  }

  return state.conversationIdsBySession[event.session_id] || event.session_id;
}

function getInboundMessageRole(payload: EventPayload): WsMessage['role'] {
  // [AutoC 2026-06-03] Why: inbound_message is also used for backend-injected
  // dispatch callbacks. How: trust the structured message_type emitted by the
  // supervisor. Purpose: reducer output no longer labels child-task callbacks as
  // ordinary user messages during realtime WebSocket delivery.
  return getString(payload.message_type) === 'dispatch_result' ? 'dispatch_callback' : 'user';
}

function buildMessageSource(state: ChatState, event: SupervisorEvent): MessageSource {
  const payload = getPayload(event);
  const taskId = getString(payload.task_id);
  const input = getRecord(payload.input);
  const childTaskId = getString(payload.child_task_id)
    || taskId
    || (input ? getString(input.child_task_id) || getString(input.inbound_child_task_id) : '');
  const childNodeId = getString(payload.child_node_id)
    || getString(payload.node_id)
    || (input ? getString(input.child_node_id) || getString(input.inbound_child_node_id) : '');
  const nodeInfo = taskId ? state.nodeByTaskId[taskId] : undefined;
  const inboundSeq = getSourceInboundSeq(payload);

  return {
    inboundSeq,
    taskId: childTaskId || undefined,
    llmRequestId: getLlmRequestId(payload) || undefined,
    childTaskId: childTaskId || undefined,
    nodeId: childNodeId || nodeInfo?.nodeId,
    childNodeId: childNodeId || undefined,
    callerNodeId: getString(payload.caller_node_id)
      || (input ? getString(input.caller_node_id) || getString(input.inbound_caller_node_id) : '')
      || undefined,
    summary: getString(payload.summary)
      || (input ? getString(input.summary) || getString(input.inbound_summary) : '')
      || undefined,
    nodeName: getString(payload.node_name) || nodeInfo?.nodeName,
    branchSessionId: getString(payload.branch_session_id) || (input ? getString(input.branch_session_id) : ''),
    parentSessionId: getString(payload.parent_session_id) || (input ? getString(input.parent_session_id) : ''),
    // [AutoC 2026-06-04] Why: downstream task events can also carry callback metadata.
    // How: preserve child-session and child/caller fields in the shared source builder.
    // Purpose: any card anchored to those events keeps the same structured navigation
    // and title data as inbound dispatch_result events.
    childSessionId: getString(payload.child_session_id) || (input ? getString(input.child_session_id) : ''),
  };
}

function getTaskNodeInfo(event: SupervisorEvent): TaskNodeInfo {
  const payload = getPayload(event);
  return {
    nodeId: getString(payload.node_id) || undefined,
    nodeName: getString(payload.node_name) || undefined,
  };
}

function mergeTaskNodeInfo(current: TaskNodeInfo | undefined, patch: TaskNodeInfo): TaskNodeInfo {
  return {
    nodeId: patch.nodeId || current?.nodeId,
    nodeName: patch.nodeName || current?.nodeName,
  };
}

function mergeSource(current: MessageSource, patch: MessageSource): MessageSource {
  return {
    inboundSeq: patch.inboundSeq !== undefined ? patch.inboundSeq : current.inboundSeq,
    taskId: patch.taskId || current.taskId,
    llmRequestId: patch.llmRequestId || current.llmRequestId,
    childTaskId: patch.childTaskId || current.childTaskId,
    nodeId: patch.nodeId || current.nodeId,
    childNodeId: patch.childNodeId || current.childNodeId,
    callerNodeId: patch.callerNodeId || current.callerNodeId,
    summary: patch.summary || current.summary,
    nodeName: patch.nodeName || current.nodeName,
    branchSessionId: patch.branchSessionId || current.branchSessionId,
    parentSessionId: patch.parentSessionId || current.parentSessionId,
    // [AutoC 2026-06-04] Why: message sources are merged across related events.
    // How: carry forward existing child/caller metadata unless a newer patch supplies
    // it. Purpose: dispatch callback navigation and titles survive later updates.
    childSessionId: patch.childSessionId || current.childSessionId,
  };
}

function appendOrMergeThinkingBlock(message: WsMessage, event: SupervisorEvent, text: string): WsMessage {
  // [2026-06-03] Only merge into the very last block if it is an active thinking
  // block. Why: scanning backwards could pull thinking text across intervening
  // text or tool blocks, breaking the natural event arrival order. How: check
  // only message.blocks[-1]. Purpose: blocks remain in the order they arrive.
  const lastBlock = message.blocks[message.blocks.length - 1];

  if (lastBlock?.kind === 'thinking' && lastBlock.streaming !== false) {
    const nextBlock: ThinkingBlock = {
      ...lastBlock,
      text: `${lastBlock.text}${text}`,
      streaming: true,
      updatedAt: event.ts,
      eventIds: appendUnique(lastBlock.eventIds, getEventId(event)),
    };
    return replaceBlock(message, nextBlock, event);
  }

  const block: ThinkingBlock = {
    id: `${message.id}|block:thinking:${getEventId(event)}`,
    kind: 'thinking',
    text,
    streaming: true,
    startedAt: event.ts,
    createdAt: event.ts,
    updatedAt: event.ts,
    eventIds: [getEventId(event)],
  };

  return {
    ...message,
    blocks: [...message.blocks, block],
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

function appendOrMergeTextBlock(
  message: WsMessage,
  event: SupervisorEvent,
  text: string,
  delivery: TextBlock['delivery'],
  streaming: boolean,
): WsMessage {
  const lastBlock = message.blocks[message.blocks.length - 1];

  if (delivery === 'stream' && lastBlock?.kind === 'text' && lastBlock.delivery === 'stream' && lastBlock.streaming !== false) {
    const nextBlock: TextBlock = {
      ...lastBlock,
      text: `${lastBlock.text}${text}`,
      streaming: true,
      updatedAt: event.ts,
      eventIds: appendUnique(lastBlock.eventIds, getEventId(event)),
    };
    return replaceBlock(message, nextBlock, event);
  }

  const block = createTextBlock({
    id: `${message.id}|block:text:${getEventId(event)}`,
    event,
    text,
    delivery,
    streaming,
  });

  return {
    ...message,
    blocks: [...message.blocks, block],
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

function createTextBlock(args: {
  id: string;
  event: SupervisorEvent;
  text: string;
  delivery: TextBlock['delivery'];
  streaming: boolean;
}): TextBlock {
  return {
    id: args.id,
    kind: 'text',
    text: args.text,
    delivery: args.delivery,
    streaming: args.streaming,
    createdAt: args.event.ts,
    updatedAt: args.event.ts,
    eventIds: [getEventId(args.event)],
  };
}

function hasStreamTextBlock(message: WsMessage): boolean {
  // Why: stream_end should preserve the active tool phase only when the message had
  // assistant text streaming. How: check for text blocks delivered through the stream
  // before finalizeStreamingBlocks clears their streaming flag. Purpose: messages do
  // not appear completed while follow-up tools are still expected.
  return message.blocks.some((block) => block.kind === 'text' && block.delivery === 'stream');
}

function finalizeStreamingBlocks(message: WsMessage, event: SupervisorEvent): WsMessage {
  let changed = false;
  const blocks = message.blocks.map((block) => {
    if ((block.kind === 'thinking' || block.kind === 'text') && block.streaming) {
      changed = true;
      // [2026-06-02] Close active ThinkingBlock timers whenever a card is finalized.
      // Why: reply-boundary cards and finish outbound cards can be finalized without a
      // stream_end event, leaving reasoning blocks visually active. How: text blocks
      // only clear streaming, while thinking blocks also receive endedAt. Purpose: the
      // UI can show a fixed elapsed time instead of an ever-running timer.
      if (block.kind === 'thinking') {
        return {
          ...block,
          streaming: false,
          endedAt: event.ts,
          updatedAt: event.ts,
          eventIds: appendUnique(block.eventIds, getEventId(event)),
        };
      }
      return {
        ...block,
        streaming: false,
        updatedAt: event.ts,
        eventIds: appendUnique(block.eventIds, getEventId(event)),
      };
    }
    return block;
  });

  if (!changed) {
    return message;
  }

  return {
    ...message,
    blocks,
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

function replaceBlock(message: WsMessage, replacement: RenderBlock, event: SupervisorEvent): WsMessage {
  return {
    ...message,
    blocks: message.blocks.map((block) => (block.id === replacement.id ? replacement : block)),
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

function replaceFirstTextBlock(blocks: readonly RenderBlock[], replacement: TextBlock): RenderBlock[] {
  let replaced = false;
  const nextBlocks = blocks.map((block) => {
    if (!replaced && block.kind === 'text') {
      replaced = true;
      return replacement;
    }
    return block;
  });

  return replaced ? nextBlocks : [replacement, ...nextBlocks];
}

function appendLegacyApprovalBlock(
  state: ChatState,
  event: SupervisorEvent,
  payload: EventPayload,
  approvalId: string,
): ChatState {
  // [AutoC 2026-05-31] Why: older backend events and cached EventLog rows may not
  // include tool_call_id. How: keep the prior ApprovalBlock path as an explicit
  // fallback. Purpose: mixed-version and historical approvals remain actionable.
  const turnKey = getTurnKey(state, event);
  let { state: nextState, message } = getOrCreateAssistantMessage(state, event, turnKey);
  const blockId = `${message.id}|approval:${approvalId}`;
  const approvalBlock: ApprovalBlock = {
    id: blockId,
    kind: 'approval',
    approvalId,
    operation: getString(payload.operation),
    details: getRecord(payload.details) || {},
    status: normalizeApprovalStatus(payload.status, payload.decision),
    decision: getString(payload.decision) || undefined,
    comment: getString(payload.comment) || undefined,
    createdAt: event.ts,
    updatedAt: event.ts,
    eventIds: [getEventId(event)],
  };

  message = setMessageStatus({
    ...message,
    blocks: appendOrReplaceApprovalBlock(message.blocks, approvalBlock),
  }, 'awaiting_approval', event);

  nextState = upsertMessage(nextState, message);
  return {
    ...nextState,
    approvalBlockById: {
      ...nextState.approvalBlockById,
      [approvalId]: { messageId: message.id, blockId },
    },
  };
}

function updateLegacyApprovalBlock(
  state: ChatState,
  event: SupervisorEvent,
  payload: EventPayload,
  approvalId: string,
  location: { messageId: string; blockId: string },
): ChatState {
  // [AutoC 2026-05-31] Why: standalone approval blocks still exist for legacy
  // payloads. How: preserve the old block update behavior behind a named fallback.
  // Purpose: the new tool-card path does not break old approvals.
  const message = state.messagesById[location.messageId];
  if (!message) {
    return state;
  }

  const decision = getString(payload.decision);
  const nextBlocks = message.blocks.map((block) => {
    if (block.kind !== 'approval' || block.id !== location.blockId) {
      return block;
    }

    return {
      ...block,
      status: normalizeApprovalStatus(payload.status, decision),
      decision: decision || block.decision,
      comment: getString(payload.comment) || block.comment,
      updatedAt: event.ts,
      eventIds: appendUnique(block.eventIds, getEventId(event)),
    } satisfies ApprovalBlock;
  });

  const nextMessage = setMessageStatus({ ...message, blocks: nextBlocks }, 'running_tools', event);
  return upsertMessage(state, nextMessage);
}

function appendOrReplaceApprovalBlock(blocks: readonly RenderBlock[], incoming: ApprovalBlock): RenderBlock[] {
  const existingIndex = blocks.findIndex((block) => block.kind === 'approval' && block.approvalId === incoming.approvalId);

  if (existingIndex < 0) {
    return [...blocks, incoming];
  }

  return blocks.map((block, index) => {
    if (index !== existingIndex || block.kind !== 'approval') {
      return block;
    }
    return {
      ...incoming,
      createdAt: block.createdAt,
      eventIds: appendUnique(block.eventIds, ...incoming.eventIds),
    };
  });
}

function setMessageStatus(message: WsMessage, status: MessageStatus, event: SupervisorEvent): WsMessage {
  if (message.status === status) {
    return {
      ...message,
      updatedAt: event.ts,
      eventIds: appendUnique(message.eventIds, getEventId(event)),
    };
  }

  return {
    ...message,
    status,
    updatedAt: event.ts,
    eventIds: appendUnique(message.eventIds, getEventId(event)),
  };
}

function resolveToolStableId(
  state: ChatState,
  message: WsMessage,
  event: SupervisorEvent,
  patch: ToolPatch,
): { stableId: string; externalKey?: string; indexKey?: string } {
  const externalId = patch.id || '';
  const externalKey = externalId ? `${message.id}|external:${externalId}` : undefined;
  const indexKey = patch.index !== undefined ? `${message.id}|index:${patch.index}` : undefined;

  if (externalKey && state.toolStableIdByExternalId[externalKey]) {
    return { stableId: state.toolStableIdByExternalId[externalKey], externalKey, indexKey };
  }

  if (indexKey && state.toolStableIdByIndex[indexKey]) {
    return { stableId: state.toolStableIdByIndex[indexKey], externalKey, indexKey };
  }

  const stableId = externalId
    ? `${message.id}|tool:id:${externalId}`
    : patch.index !== undefined
      ? `${message.id}|tool:index:${patch.index}`
      : `${message.id}|tool:event:${getEventId(event)}`;

  return { stableId, externalKey, indexKey };
}

function normalizeToolStatus(value: unknown): ToolStatus {
  const raw = getString(value);
  if (raw === 'async_started' || raw === 'success' || raw === 'error' || raw === 'cancelled') {
    return raw;
  }
  if (raw === 'running' || raw === 'queued' || raw === 'args_streaming' || raw === 'awaiting_approval') {
    return raw;
  }
  return 'success';
}

function normalizeApprovalStatus(status: unknown, decision: unknown): ApprovalBlock['status'] {
  const rawStatus = getString(status);
  const rawDecision = getString(decision);

  if (rawStatus === 'allowed' || rawDecision === 'allow') {
    return 'allowed';
  }
  if (rawStatus === 'denied' || rawDecision === 'deny') {
    return 'denied';
  }
  return 'pending';
}

function findToolByCallId(state: ChatState, toolCallId: string): ToolExecution | undefined {
  // [AutoC 2026-05-31] Why: approval events carry the provider tool_call_id, not
  // the reducer's stable id. How: scan the normalized tool table for the external
  // id field. Purpose: approvals can attach to tools regardless of block location.
  return Object.values(state.toolExecutionsById).find((tool) => tool.id === toolCallId);
}

function buildApprovalDetails(payload: EventPayload): Record<string, unknown> {
  // [AutoC 2026-05-31] Why: ToolExecution needs enough approval data to render
  // operation, path, and reason without reconstructing the old ApprovalBlock. How:
  // retain both operation and raw details under one object. Purpose: ToolCallCard
  // can show the same meaningful fields as the previous standalone card.
  return {
    operation: getString(payload.operation),
    details: getRecord(payload.details) || {},
  };
}

function getToolStatusAfterApproval(
  currentStatus: ToolStatus,
  approvalStatus: ApprovalBlock['status'],
): ToolStatus {
  // [AutoC 2026-05-31] Why: approval_decided should not overwrite a result that
  // has already arrived during replay or reconnect. How: preserve terminal statuses
  // and only move pending approval back into running/error. Purpose: event order
  // remains robust while the card reflects the decision immediately.
  if (TERMINAL_TOOL_STATUSES.has(currentStatus)) return currentStatus;
  if (approvalStatus === 'allowed') return 'running';
  if (approvalStatus === 'denied') return 'error';
  return 'awaiting_approval';
}

function isLogOnlyEvent(type: string): boolean {
  return LOG_ONLY_EVENTS.has(type)
    || type.startsWith('compact_')
    || type.startsWith('preempt_')
    || type === 'snip_compact';
}

function summarizeEvent(event: SupervisorEvent): string | undefined {
  const payload = getPayload(event);

  if (event.type === 'handoff_progress') {
    return getString(payload.message) || undefined;
  }
  if (event.type === 'llm_retry') {
    return getString(payload.error) || undefined;
  }
  if (event.type === 'tool_call_start') {
    return getString(payload.tool_name) || undefined;
  }
  if (event.type === 'tool_call_end') {
    return getString(payload.summary) || getString(payload.tool_name) || undefined;
  }
  if (event.type === 'node_switch') {
    return getString(payload.target_node_id) || 'default';
  }
  if (event.type === 'approval_requested') {
    return getString(payload.operation) || undefined;
  }

  return undefined;
}

function getPayload(event: SupervisorEvent): EventPayload {
  return getRecord(event.payload) || {};
}

function getRecord(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return undefined;
  }
  return value as Record<string, unknown>;
}

function getString(value: unknown): string {
  return typeof value === 'string' ? value : value === undefined || value === null ? '' : String(value);
}

function getNumber(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function getBoolean(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined;
}

function getLlmRequestId(payload: EventPayload): string {
  const nestedResult = getRecord(payload.result);
  // [AutoC 2026-06-04] Why: task_completed stores engine output under result while
  // realtime stream and outbound events put the id at the payload top level. How:
  // read both shapes plus a conservative request_id alias for mixed-version logs.
  // Purpose: reducer card lookup remains request-scoped across live and terminal events.
  return getString(payload.llm_request_id)
    || getString(payload.request_id)
    || (nestedResult ? getString(nestedResult.llm_request_id) || getString(nestedResult.request_id) : '');
}

function getSourceInboundSeq(payload: EventPayload): number | undefined {
  const seq = getNumber(payload.source_inbound_seq);
  return seq !== undefined && seq > 0 ? seq : undefined;
}

function hasSourceInboundSeq(payload: EventPayload): boolean {
  return getSourceInboundSeq(payload) !== undefined;
}

function getAttachments(value: unknown): Attachment[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value
    .filter((item): item is Record<string, unknown> => Boolean(getRecord(item)))
    .map((item) => {
      const record = item as Record<string, unknown>;
      return {
        name: getString(record.name),
        size: getNumber(record.size),
        url: getString(record.url) || undefined,
        type: getString(record.type) || undefined,
        path: getString(record.path) || undefined,
        mime_type: getString(record.mime_type) || undefined,
      };
    });
}

function normalizeConversationKey(value: string): string {
  if (!value) {
    return '';
  }
  return value.startsWith('web:') ? value.slice(4) : value;
}

function getEventId(event: SupervisorEvent): string {
  return event.event_id || `${event.session_id}:${event.seq}:${event.type}`;
}

function getUserMessageId(conversationId: string, inboundSeq: number): string {
  return `message:${conversationId}:user:inbound:${inboundSeq}`;
}

function getAssistantMessageId(conversationId: string, turnKey: string): string {
  return `message:${conversationId}:assistant:${turnKey}`;
}

function appendUnique<T>(items: readonly T[], ...incoming: T[]): T[] {
  const next = [...items];
  for (const item of incoming) {
    if (!next.includes(item)) {
      next.push(item);
    }
  }
  return next;
}

function stringifyJson(value: Record<string, unknown>): string {
  try {
    return JSON.stringify(value);
  } catch {
    return '';
  }
}

function parseJsonRecord(value: string | undefined): Record<string, unknown> | undefined {
  if (!value) {
    return undefined;
  }

  try {
    return getRecord(JSON.parse(value));
  } catch {
    return undefined;
  }
}
