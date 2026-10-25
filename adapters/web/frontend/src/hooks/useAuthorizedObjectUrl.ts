// 带令牌取回一个文件并换成 object URL；换地址、换令牌或卸载时中止请求并释放上一份。
import { useEffect, useState } from 'react';

import { fetchBlob } from '../api/blobs';
import { useSettingsStore } from '../store/settingsStore';

export interface AuthorizedObjectUrl {
  url: string;
  /** 响应的 Content-Type，判断能不能在新标签页里直接打开。 */
  type: string;
  loading: boolean;
  error: unknown;
}

const IDLE: AuthorizedObjectUrl = { url: '', type: '', loading: false, error: null };

/** source 为 null 或 enabled 为 false 时不取。 */
export function useAuthorizedObjectUrl(source: string | null, enabled = true): AuthorizedObjectUrl {
  const token = useSettingsStore((state) => state.adminToken);
  const [state, setState] = useState<AuthorizedObjectUrl>(IDLE);

  useEffect(() => {
    if (!source || !enabled) {
      setState(IDLE);
      return undefined;
    }
    const controller = new AbortController();
    let objectUrl = '';
    setState({ ...IDLE, loading: true });
    fetchBlob(source, { signal: controller.signal, token })
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setState({ url: objectUrl, type: blob.type, loading: false, error: null });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setState({ ...IDLE, error });
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [source, enabled, token]);

  return state;
}
