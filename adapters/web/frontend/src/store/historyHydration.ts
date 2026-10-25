// /history 的结构化记录还原成和实时事件同一套消息与工具卡，再与会话里已有的消息合并。
import type { StructuredMessage, StructuredThinkingBlock } from '../api/supervisorClient';
import type {
  ChatState,
  RenderBlock,
  TextBlock,
  ThinkingBlock,
  ToolBlock,
  ToolExecution,
  ToolStatus,
  WsMessage,
} from '../types/message';

const CONTROL_TOOL_NAMES = new Set(['finish', 'reply', 'switch_node', 'ask']);
const INTERNAL_USER_MESSAGE_TYPES = new Set(['tool_result', 'system', 'summary']);
// 只能按文字认同一条时，历史记录不能比实时消息早太多；乐观插入的那条用的是浏览器时钟，留出时钟偏差。
const LIVE_MATCH_SKEW_MS = 10 * 60_000;

export interface HistoryEntry {
  message: WsMessage;
  tools: ToolExecution[];
  /** 拼成这条消息的原始记录。 */
  rowIds: string[];
  /** 这些记录的模型请求 id，实时卡片靠它认出自己落盘后的样子。 */
  requestIds: string[];
}

export interface HistoryWindow {
  /** 已载入的连续一段记录，按完整历史里的先后排。 */
  rows: StructuredMessage[];
  hasMore: boolean;
}

export interface HistoryMergeOptions {
  /** true：实时消息一条不删，历史只补缺；false：历史整体替换这个会话的消息。 */
  keepLive: boolean;
  /** 这次新出现的记录。只有它们可能是某条实时消息落盘后的样子。 */
  freshRowIds: ReadonlySet<string>;
  /** 上次合并时实时消息认领的历史消息（实时消息 id → 历史消息 id）。 */
  anchors: ReadonlyMap<string, string>;
}

export interface HistoryMergeResult<S extends ChatState> {
  state: S;
  anchors: Map<string, string>;
}

type ToolResult = {
  status: 'success' | 'error';
  result?: string;
  rawInline?: string;
  rejected?: boolean;
  isAutoResult?: boolean;
};

type HistoryToolCall = {
  id?: string;
  name: string;
  arguments?: Record<string, unknown>;
};

type ThinkingSegment = {
  text: string;
  startedAt?: string;
  endedAt?: string;
};

type PendingAssistant = {
  thinking: ThinkingSegment[];
  tools: ToolExecution[];
  createdAt: string;
  rowIds: string[];
  requestIds: string[];
};

/** 记录在窗口里的身份。老数据没有 id 时退到 cursor。 */
export function historyRowKey(row: StructuredMessage): string {
  return row.id || (typeof row.cursor === 'number' ? `at-${row.cursor}` : '');
}

export function historyWindowOf(rows: StructuredMessage[]): HistoryWindow {
  const first = rows[0]?.cursor;
  return { rows, hasMore: typeof first === 'number' && first > 0 };
}

/** 最新一页回来时接上已载入的更早记录；接不上（中间缺了，或同一位置换了记录）就只留这一页。 */
export function mergeLatestHistoryPage(previous: HistoryWindow | undefined, page: readonly StructuredMessage[]): HistoryWindow {
  const rows = [...page];
  const firstCursor = rows[0]?.cursor;
  if (!previous || typeof firstCursor !== 'number') return historyWindowOf(rows);
  const older = previous.rows.filter((row) => typeof row.cursor === 'number' && row.cursor < firstCursor);
  if (older.length === 0) return historyWindowOf(rows);
  const idAtCursor = new Map(previous.rows.map((row) => [row.cursor, row.id]));
  const consistent = rows.every((row) => !idAtCursor.has(row.cursor) || idAtCursor.get(row.cursor) === row.id);
  const contiguous = older[older.length - 1].cursor === firstCursor - 1;
  return historyWindowOf(consistent && contiguous ? [...older, ...rows] : rows);
}

/** 往前翻到的一页接在最前面。 */
export function mergeOlderHistoryPage(previous: HistoryWindow, page: readonly StructuredMessage[]): HistoryWindow {
  const firstLoaded = previous.rows[0]?.cursor;
  const known = new Set(previous.rows.map(historyRowKey));
  const older = page.filter((row) => typeof row.cursor === 'number'
    && (typeof firstLoaded !== 'number' || row.cursor < firstLoaded)
    && !known.has(historyRowKey(row)));
  // 往前翻却没拿到更早的：历史被压缩或重排过，再翻也还是空。
  if (older.length === 0) return { rows: previous.rows, hasMore: false };
  return historyWindowOf([...older, ...previous.rows]);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringifyContent(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function collapse(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function truncate(value: string, limit: number): string {
  const chars = Array.from(value);
  return chars.length > limit ? `${chars.slice(0, limit).join('')}…` : value;
}

function summarizeArguments(toolName: string, args: Record<string, unknown> | undefined): string {
  if (!args || Object.keys(args).length === 0) return '';
  if (CONTROL_TOOL_NAMES.has(toolName) && typeof args.text === 'string') return '';
  return Object.entries(args)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${truncate(collapse(stringifyContent(value)), 80)}`)
    .join(', ');
}

function buildToolResultIndex(rows: readonly StructuredMessage[]): Map<string, ToolResult> {
  const index = new Map<string, ToolResult>();
  for (const row of rows) {
    if (row.role !== 'tool' || !row.tool_call_id) continue;
    const toolName = row.tool_name || row.name || '';
    const rawContent = stringifyContent(row.content);
    const trimmed = rawContent.trim();
    // 老记录的拒绝结果前面带一个叉号。
    const rejected = /^\s*(?:❌\s*)?REJECTED(?:\b|:)/i.test(trimmed);
    const status: ToolResult['status'] = rejected || /^ERROR(?:\b|:)/i.test(trimmed) ? 'error' : 'success';
    const isAutoResult = status === 'success' && CONTROL_TOOL_NAMES.has(toolName) && trimmed.toLowerCase() === 'ok';
    index.set(row.tool_call_id, {
      status,
      rawInline: rawContent,
      rejected: rejected || undefined,
      isAutoResult: isAutoResult || undefined,
      result: isAutoResult
        ? undefined
        : status === 'error'
          ? rawContent
          : truncate(collapse(rawContent), 120) || undefined,
    });
  }
  return index;
}

function normalizeToolCalls(value: StructuredMessage['tool_calls']): HistoryToolCall[] {
  if (!Array.isArray(value)) return [];
  return value.map((toolCall, index) => ({
    id: toolCall.id || `history-tool-${index}`,
    name: toolCall.name || 'unknown',
    arguments: isRecord(toolCall.arguments) ? toolCall.arguments : undefined,
  }));
}

function normalizeThinkingSegments(row: StructuredMessage): ThinkingSegment[] {
  const normalizeBlock = (block: StructuredThinkingBlock): ThinkingSegment | null => {
    const text = typeof block.text === 'string' ? block.text : '';
    if (!text.trim()) return null;
    return {
      text,
      startedAt: typeof block.started_at === 'string' ? block.started_at : typeof block.startedAt === 'string' ? block.startedAt : undefined,
      endedAt: typeof block.ended_at === 'string' ? block.ended_at : typeof block.endedAt === 'string' ? block.endedAt : undefined,
    };
  };

  const structured = Array.isArray(row.thinking_blocks)
    ? row.thinking_blocks.map(normalizeBlock).filter((block): block is ThinkingSegment => Boolean(block))
    : [];
  if (structured.length > 0) return structured;

  if (isRecord(row.thinking)) {
    const normalized = normalizeBlock(row.thinking as StructuredThinkingBlock);
    return normalized ? [normalized] : [];
  }

  // 老记录把思考过程放成一段字符串，计时在顶层字段上。
  const text = typeof row.thinking === 'string' ? row.thinking : row.thinking_text || '';
  if (!text.trim()) return [];
  return [{ text, startedAt: row.reasoning_started_at, endedAt: row.reasoning_ended_at }];
}

function extractControlText(toolCalls: readonly HistoryToolCall[]): { text: string; toolName?: string } {
  for (const toolCall of toolCalls) {
    if (!CONTROL_TOOL_NAMES.has(toolCall.name)) continue;
    const text = typeof toolCall.arguments?.text === 'string' ? toolCall.arguments.text : '';
    if (text) return { text, toolName: toolCall.name };
  }
  return { text: '' };
}

function toolStatusOf(result: ToolResult | undefined): ToolStatus {
  return result ? result.status : 'success';
}

function createToolExecutions(
  toolCalls: readonly HistoryToolCall[],
  results: Map<string, ToolResult>,
  messageId: string,
  createdAt: string,
  nodeId?: string,
): ToolExecution[] {
  return toolCalls.map((toolCall, index) => {
    const result = toolCall.id ? results.get(toolCall.id) : undefined;
    return {
      stableId: `${messageId}|history-tool:${toolCall.id || index}`,
      messageId,
      id: toolCall.id,
      index,
      name: toolCall.name,
      status: toolStatusOf(result),
      arguments: toolCall.arguments,
      argumentsText: toolCall.arguments ? JSON.stringify(toolCall.arguments) : undefined,
      summary: summarizeArguments(toolCall.name, toolCall.arguments),
      result: result?.result,
      rawInline: result?.rawInline,
      rejected: result?.rejected,
      // 控制工具自动回的 ok 不用看；被拒绝的要留着，它说明了最后那一步为什么没成。
      hidden: CONTROL_TOOL_NAMES.has(toolCall.name) && result?.isAutoResult && !result?.rejected,
      nodeId,
      createdAt,
      updatedAt: createdAt,
      eventIds: [],
    } satisfies ToolExecution;
  });
}

function textBlock(id: string, createdAt: string, text: string, delivery: TextBlock['delivery'] = 'history'): TextBlock {
  return { id, kind: 'text', text, delivery, streaming: false, createdAt, updatedAt: createdAt, eventIds: [`${id}:history`] };
}

function thinkingBlock(id: string, createdAt: string, segment: ThinkingSegment): ThinkingBlock {
  return {
    id,
    kind: 'thinking',
    text: segment.text,
    streaming: false,
    startedAt: segment.startedAt,
    endedAt: segment.endedAt,
    createdAt,
    updatedAt: createdAt,
    eventIds: [`${id}:history`],
  };
}

function toolBlock(id: string, createdAt: string, toolIds: readonly string[]): ToolBlock {
  return { id, kind: 'tool', toolIds: [...toolIds], createdAt, updatedAt: createdAt, eventIds: [`${id}:history`] };
}

function isDispatchResult(row: StructuredMessage): boolean {
  // async_dispatch: 前缀是 message_type 出现之前的老记录。
  return row.message_type === 'dispatch_result' || row.id.startsWith('async_dispatch:');
}

function emptyPending(): PendingAssistant {
  return { thinking: [], tools: [], createdAt: '', rowIds: [], requestIds: [] };
}

function appendUnique(items: readonly string[], ...incoming: string[]): string[] {
  const next = [...items];
  for (const item of incoming) if (item && !next.includes(item)) next.push(item);
  return next;
}

export function buildHistoryEntries(rows: readonly StructuredMessage[], sessionId: string, conversationId: string): HistoryEntry[] {
  const results = buildToolResultIndex(rows);
  const entries: HistoryEntry[] = [];
  let pending = emptyPending();

  const pushAssistant = (
    row: StructuredMessage,
    key: string,
    ownRowId: string,
    text: string,
    currentTools: readonly ToolExecution[],
    completionType?: WsMessage['completionType'],
  ) => {
    const createdAt = row.created_at || pending.createdAt || new Date().toISOString();
    const messageId = `message:${conversationId}:history:${key}`;
    const requestIds = appendUnique(pending.requestIds, row.llm_request_id || '');
    const blocks: RenderBlock[] = [...pending.thinking, ...normalizeThinkingSegments(row)]
      .filter((segment) => segment.text.trim())
      .map((segment, index) => thinkingBlock(`${messageId}|block:thinking:history:${index}`, createdAt, segment));
    // 只有工具调用的那几条记录并进后面第一条有字的消息里，工具卡跟着换主人。
    let tools = [...pending.tools, ...currentTools].map((tool) => ({ ...tool, messageId }));
    if (tools.length > 0) {
      const blockId = `${messageId}|block:tool:history`;
      blocks.push(toolBlock(blockId, createdAt, tools.map((tool) => tool.stableId)));
      tools = tools.map((tool) => ({ ...tool, blockId, eventIds: [`history:${sessionId}:${messageId}:tool:${tool.index ?? 0}`] }));
    }
    if (text) {
      const delivery: TextBlock['delivery'] = completionType === 'reply' ? 'intermediate' : completionType === 'finish' ? 'final' : 'history';
      blocks.push(textBlock(`${messageId}|block:text:history`, createdAt, text, delivery));
    }
    entries.push({
      message: {
        id: messageId,
        conversationId,
        sessionId,
        role: 'assistant',
        status: 'completed',
        createdAt,
        updatedAt: createdAt,
        source: {
          llmRequestId: row.llm_request_id || requestIds[requestIds.length - 1] || undefined,
          taskId: row.source_task_id || undefined,
          nodeId: row.source_node_id || undefined,
        },
        blocks,
        eventIds: [`history:${sessionId}:${row.id || messageId}`],
        hydratedFromHistory: true,
        ...(completionType && { completionType }),
      },
      tools,
      rowIds: appendUnique(pending.rowIds, ownRowId),
      requestIds,
    });
    pending = emptyPending();
  };

  // 一轮以只调工具的记录收尾（任务还在跑或中途停了）：单独出一条空消息，免得这些工具卡丢掉。
  const flushPending = () => {
    if (pending.thinking.length === 0 && pending.tools.length === 0) return;
    const id = `dangling-${pending.rowIds[0] || entries.length}`;
    pushAssistant({ id, role: 'assistant', content: '', created_at: pending.createdAt || undefined }, id, '', '', []);
  };

  rows.forEach((row, index) => {
    const rowId = historyRowKey(row);
    const key = rowId || `index-${index}`;

    if (row.role === 'user') {
      flushPending();
      if (INTERNAL_USER_MESSAGE_TYPES.has(row.message_type || '')) return;
      const createdAt = row.created_at || new Date().toISOString();
      const messageId = `message:${conversationId}:history:${key}`;
      const text = stringifyContent(row.content);
      entries.push({
        message: {
          id: messageId,
          conversationId,
          sessionId,
          role: isDispatchResult(row) ? 'dispatch_callback' : 'user',
          status: 'completed',
          createdAt,
          updatedAt: createdAt,
          source: {
            nodeId: row.child_node_id || row.dispatch_node_id || row.source_node_id || undefined,
            childNodeId: row.child_node_id || row.dispatch_node_id || undefined,
            taskId: row.child_task_id || row.dispatch_task_id || row.source_task_id || undefined,
            childTaskId: row.child_task_id || row.dispatch_task_id || undefined,
            callerNodeId: row.caller_node_id || undefined,
            summary: row.summary || undefined,
            childSessionId: row.child_session_id || undefined,
          },
          blocks: [textBlock(`${messageId}|block:text:history`, createdAt, text)],
          eventIds: [`history:${sessionId}:${row.id || messageId}`],
          hydratedFromHistory: true,
        },
        tools: [],
        rowIds: rowId ? [rowId] : [],
        requestIds: [],
      });
      return;
    }

    if (row.role !== 'assistant') return;

    const createdAt = row.created_at || new Date().toISOString();
    const toolCalls = normalizeToolCalls(row.tool_calls);
    const currentTools = createToolExecutions(toolCalls, results, `message:${conversationId}|history-pending:${row.id || 'assistant'}`, createdAt, row.source_node_id);
    const control = extractControlText(toolCalls);
    const content = stringifyContent(row.content);
    const hasControlTool = toolCalls.some((toolCall) => CONTROL_TOOL_NAMES.has(toolCall.name));

    if (hasControlTool && control.text) {
      // finish、reply、ask、switch_node 各自单独落一条；正文只是模型的草稿，以控制工具里的文字为准。
      flushPending();
      const completionType: WsMessage['completionType'] = control.toolName === 'finish'
        ? 'finish'
        : control.toolName === 'ask'
          ? 'ask'
          : control.toolName === 'reply'
            ? 'reply'
            : undefined;
      pushAssistant(row, key, rowId, control.text, currentTools, completionType);
      return;
    }

    const displayText = content.trim() ? content : control.text;
    if (!displayText && !hasControlTool) {
      pending.thinking.push(...normalizeThinkingSegments(row));
      pending.tools.push(...currentTools);
      if (!pending.createdAt) pending.createdAt = row.created_at || '';
      if (rowId) pending.rowIds.push(rowId);
      if (row.llm_request_id) pending.requestIds = appendUnique(pending.requestIds, row.llm_request_id);
      return;
    }

    pushAssistant(row, key, rowId, displayText, currentTools);
  });

  flushPending();
  return entries;
}

function textSignature(message: WsMessage): string {
  const text = message.blocks
    .filter((block): block is TextBlock => block.kind === 'text')
    .map((block) => block.text)
    .join('\n');
  const compact = collapse(text);
  return compact ? `${message.role}:${compact}` : '';
}

function closeInTime(liveAt: string, historyAt: string): boolean {
  const live = Date.parse(liveAt);
  const history = Date.parse(historyAt);
  if (Number.isNaN(live) || Number.isNaN(history)) return true;
  return history >= live - LIVE_MATCH_SKEW_MS;
}

function isFreshEntry(entry: HistoryEntry, freshRowIds: ReadonlySet<string>): boolean {
  return entry.rowIds.length === 0 || entry.rowIds.some((rowId) => freshRowIds.has(rowId));
}

function matchesByRequest(live: WsMessage, entry: HistoryEntry): boolean {
  return Boolean(live.source.llmRequestId) && entry.requestIds.length > 0;
}

function representsSameTurn(live: WsMessage, entry: HistoryEntry, textTaken: boolean): boolean {
  const history = entry.message;
  if (live.role !== history.role) return false;
  // 几个模型请求的实时卡片在历史里可能并成一条，所以按请求 id 可以多对一。
  if (matchesByRequest(live, entry)) return entry.requestIds.includes(live.source.llmRequestId || '');
  if (textTaken) return false;
  const signature = textSignature(live);
  return Boolean(signature) && signature === textSignature(history) && closeInTime(live.createdAt, history.createdAt);
}

function claimEntries(
  live: readonly WsMessage[],
  entries: readonly HistoryEntry[],
  options: HistoryMergeOptions,
): { byEntry: Map<number, number[]>; anchors: Map<string, string> } {
  const byEntry = new Map<number, number[]>();
  const anchors = new Map<string, string>();
  const entryIndexById = new Map(entries.map((entry, index) => [entry.message.id, index]));
  const textTaken = new Set<number>();
  const claim = (entryIndex: number, liveIndex: number) => {
    const claimers = byEntry.get(entryIndex);
    if (claimers) claimers.push(liveIndex);
    else byEntry.set(entryIndex, [liveIndex]);
    anchors.set(live[liveIndex].id, entries[entryIndex].message.id);
    if (!matchesByRequest(live[liveIndex], entries[entryIndex])) textTaken.add(entryIndex);
  };

  live.forEach((message, liveIndex) => {
    const anchored = options.anchors.get(message.id);
    const anchoredIndex = anchored === undefined ? undefined : entryIndexById.get(anchored);
    if (anchoredIndex !== undefined) {
      claim(anchoredIndex, liveIndex);
      return;
    }
    const entryIndex = entries.findIndex((entry, index) => isFreshEntry(entry, options.freshRowIds)
      && representsSameTurn(message, entry, textTaken.has(index)));
    if (entryIndex >= 0) claim(entryIndex, liveIndex);
  });
  return { byEntry, anchors };
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function sameIds(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index]);
}

// 审批只在实时事件里，不进历史；重建工具卡时沿用，已经有结果的工具不会再等审批。
function carryApproval(tool: ToolExecution, previous: ToolExecution | undefined): ToolExecution {
  if (!previous?.approvalId) return tool;
  const finished = tool.rawInline !== undefined;
  if (finished && previous.approvalStatus === 'pending') return tool;
  return {
    ...tool,
    status: finished ? tool.status : previous.status,
    approvalId: previous.approvalId,
    approvalStatus: previous.approvalStatus,
    approvalDetails: previous.approvalDetails,
  };
}

/** 把一组历史消息放进会话。没有任何变化时原样返回 state，订阅方不会被惊动。 */
export function mergeHistoryEntries<S extends ChatState>(
  state: S,
  conversationId: string,
  entries: readonly HistoryEntry[],
  options: HistoryMergeOptions,
): HistoryMergeResult<S> {
  const previousOrder = state.messageOrderByConversation[conversationId] || [];
  const previous = previousOrder
    .map((id) => state.messagesById[id])
    .filter((message): message is WsMessage => Boolean(message));
  const live = options.keepLive ? previous.filter((message) => !message.hydratedFromHistory) : [];
  const { byEntry, anchors } = claimEntries(live, entries, options);

  const claimedLive = new Set([...byEntry.values()].flat());
  const emitted = new Set<number>();
  const order: string[] = [];
  const shown: HistoryEntry[] = [];
  const emitLive = (index: number) => {
    if (emitted.has(index)) return;
    emitted.add(index);
    order.push(live[index].id);
  };
  // 被认领的历史消息换成实时那几张卡片；排在它们前面、没被认领的实时消息先放出来，保持实时消息之间的先后。
  entries.forEach((entry, entryIndex) => {
    const claimers = byEntry.get(entryIndex);
    if (!claimers) {
      order.push(entry.message.id);
      shown.push(entry);
      return;
    }
    const first = Math.min(...claimers);
    for (let index = 0; index < first; index += 1) if (!claimedLive.has(index)) emitLive(index);
    claimers.forEach(emitLive);
  });
  live.forEach((_, index) => emitLive(index));

  const kept = new Set(order);
  const rebuiltOwners = new Set<string>();
  let messagesChanged = false;
  const messagesById: Record<string, WsMessage> = { ...state.messagesById };
  for (const message of previous) {
    if (kept.has(message.id)) continue;
    delete messagesById[message.id];
    rebuiltOwners.add(message.id);
    messagesChanged = true;
  }
  for (const entry of shown) {
    rebuiltOwners.add(entry.message.id);
    const existing = state.messagesById[entry.message.id];
    if (existing && sameValue(existing, entry.message)) continue;
    messagesById[entry.message.id] = entry.message;
    messagesChanged = true;
  }

  const rebuiltTools = new Map<string, ToolExecution>();
  for (const entry of shown) {
    for (const tool of entry.tools) {
      const existing = state.toolExecutionsById[tool.stableId];
      const next = carryApproval(tool, existing);
      rebuiltTools.set(tool.stableId, existing && sameValue(existing, next) ? existing : next);
    }
  }
  let toolsChanged = false;
  const toolExecutionsById: Record<string, ToolExecution> = { ...state.toolExecutionsById };
  for (const [toolId, tool] of Object.entries(state.toolExecutionsById)) {
    if (!rebuiltOwners.has(tool.messageId) || rebuiltTools.has(toolId)) continue;
    delete toolExecutionsById[toolId];
    toolsChanged = true;
  }
  for (const [toolId, tool] of rebuiltTools) {
    if (toolExecutionsById[toolId] === tool) continue;
    toolExecutionsById[toolId] = tool;
    toolsChanged = true;
  }

  const orderChanged = !sameIds(order, previousOrder);
  if (!messagesChanged && !toolsChanged && !orderChanged) return { state, anchors };

  let toolExecutionOrder = state.toolExecutionOrder;
  if (toolsChanged) {
    const listed = new Set<string>();
    const nextOrder = state.toolExecutionOrder.filter((toolId) => {
      if (!(toolId in toolExecutionsById)) return false;
      listed.add(toolId);
      return true;
    });
    for (const toolId of rebuiltTools.keys()) if (!listed.has(toolId)) nextOrder.push(toolId);
    toolExecutionOrder = nextOrder;
  }

  return {
    state: {
      ...state,
      messagesById: messagesChanged ? messagesById : state.messagesById,
      messageOrderByConversation: orderChanged
        ? { ...state.messageOrderByConversation, [conversationId]: order }
        : state.messageOrderByConversation,
      toolExecutionsById: toolsChanged ? toolExecutionsById : state.toolExecutionsById,
      toolExecutionOrder,
    },
    anchors,
  };
}
