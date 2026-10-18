import { createContext, createElement, useCallback, useContext, useEffect, useState, type PropsWithChildren } from 'react';

import { getInstanceDirectory, MOUNT, type InstanceDirectoryEntry } from '../../api/supervisorClient';

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

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setState(initialState);
    void getInstanceDirectory(controller.signal).then(result => {
      if (!controller.signal.aborted) setState({ status: 'ready', currentPath: result.current_path, instances: result.instances });
    }).catch(() => {
      if (!controller.signal.aborted) setState({ ...initialState, status: 'error' });
    });
    return () => controller.abort();
  }, [enabled, revision]);

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
