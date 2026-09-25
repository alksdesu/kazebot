import { useCallback, useEffect, useRef, useState } from 'react';
import { getConversations, type ConversationRow } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';

export function useConversationDirectory() {
  const token = useSettingsStore(state => state.adminToken);
  const sequence = useRef(0);
  const [snapshot, setSnapshot] = useState<{ token: string | null; rows: ConversationRow[]; error: string; loading: boolean }>({ token, rows: [], error: '', loading: false });
  const reload = useCallback(async () => {
    const request = ++sequence.current;
    setSnapshot({ token, rows: [], error: '', loading: Boolean(token) });
    if (!token) return;
    try {
      const result = await getConversations(token);
      if (request !== sequence.current || useSettingsStore.getState().adminToken !== token) return;
      if (!Array.isArray(result)) throw new Error('invalid conversations');
      const rows = result.filter(row => row && typeof row.conversation_key === 'string');
      setSnapshot({ token, rows, error: '', loading: false });
    } catch {
      if (request === sequence.current && useSettingsStore.getState().adminToken === token) setSnapshot({ token, rows: [], error: '会话列表读取失败，仍可操作当前会话。', loading: false });
    }
  }, [token]);
  useEffect(() => { void reload(); return () => { sequence.current++; }; }, [reload]);
  return { rows: snapshot.token === token ? snapshot.rows : [], error: snapshot.token === token ? snapshot.error : '', loading: snapshot.token === token ? snapshot.loading : Boolean(token), reload };
}
