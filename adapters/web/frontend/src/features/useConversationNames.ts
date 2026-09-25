import { useMemo } from 'react';
import { useChatStore } from '../store/chatStore';
import { conversationNames, type ConversationDescriptor } from './conversationNames';

export function useConversationNames(rows: readonly ConversationDescriptor[], includeWebTitles = true, titleScopes?: readonly string[]) {
  const conversations = useChatStore(state => state.conversations);
  return useMemo(() => {
    const allowed = new Set(titleScopes || rows.map(row => row.scope));
    const titles = new Map(conversations.filter(item => allowed.has(`web:${item.id}`)).map(item => [`web:${item.id}`, item.title]));
    return conversationNames(rows, includeWebTitles ? titles : undefined);
  }, [rows, conversations, includeWebTitles, titleScopes]);
}
