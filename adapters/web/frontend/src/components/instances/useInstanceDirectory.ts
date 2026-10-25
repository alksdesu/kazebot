import { createContext, createElement, useCallback, useContext, useEffect, useState, type PropsWithChildren } from 'react';

import { getInstanceDirectory, MOUNT, type InstanceDirectoryEntry } from '../../api/supervisorClient';
import { useRequestGuard } from '../../hooks/useRequestGuard';

interface DirectoryState {
  status: 'loading' | 'ready' | 'error';
  currentPath: string;
  instances: InstanceDirectoryEntry[];
}

const initialState: DirectoryState = { status: 'loading', currentPath: MOUNT, instances: [] };
const DirectoryContext = createContext<(DirectoryState & { retry: () => void }) | null>(null);

function useDirectoryRequest(enabled: boolean) {
  const [state, setState] = useState<DirectoryState>(initialState);
  const [revision, setRevision] = useState(0);
  const retry = useCallback(() => setRevision(value => value + 1), []);
  const requests = useRequestGuard('', { anonymous: true });

  useEffect(() => {
    if (!enabled) return;
    const request = requests.begin('directory');
    setState(initialState);
    void getInstanceDirectory(request.signal).then(result => {
      if (request.isCurrent()) setState({ status: 'ready', currentPath: result.current_path, instances: result.instances });
    }).catch(() => {
      if (request.isCurrent()) setState({ ...initialState, status: 'error' });
    });
    return () => requests.drop('directory');
  }, [requests, enabled, revision]);

  return { ...state, retry };
}

export function InstanceDirectoryProvider({ children }: PropsWithChildren) {
  const directory = useDirectoryRequest(true);
  return createElement(DirectoryContext.Provider, { value: directory }, children);
}

export function useInstanceDirectory() {
  const shared = useContext(DirectoryContext);
  const local = useDirectoryRequest(!shared);
  return shared ?? local;
}
