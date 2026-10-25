// [2026-05-31] Selectors for the reducer-backed chat state.
// Why: render components should consume ordered messages, tools, and logs without
// knowing the reducer's normalization tables. How: derive arrays from stable order
// indexes and filter missing IDs defensively. Purpose: later UI steps can switch
// to selector output without coupling React components to event replay internals.
import type { ChatState, EventLogEntry, ToolExecution, WsMessage } from '../types/message';

export function selectMessages(state: ChatState, conversationId: string): WsMessage[] {
  const order = state.messageOrderByConversation[conversationId] || [];
  return order
    .map((messageId) => state.messagesById[messageId])
    .filter((message): message is WsMessage => Boolean(message));
}

export function selectToolExecutions(state: ChatState, toolIds: readonly string[]): ToolExecution[] {
  return toolIds
    .map((toolId) => state.toolExecutionsById[toolId])
    .filter((tool): tool is ToolExecution => Boolean(tool) && !tool.hidden);
}

/** The tools the given messages' tool blocks point at, so streams in other conversations leave the result shallow-equal. */
export function selectToolsForMessages(state: ChatState, messages: readonly WsMessage[]): Record<string, ToolExecution> {
  const tools: Record<string, ToolExecution> = {};
  for (const message of messages) {
    for (const block of message.blocks) {
      if (block.kind !== 'tool') continue;
      for (const toolId of block.toolIds) {
        const tool = state.toolExecutionsById[toolId];
        if (tool) tools[toolId] = tool;
      }
    }
  }
  return tools;
}

export function selectEventLog(state: ChatState, sessionId: string, limit = 200): EventLogEntry[] {
  const filtered = state.eventLog.filter((entry) => entry.sessionId === sessionId);
  return limit > 0 ? filtered.slice(-limit) : filtered;
}
