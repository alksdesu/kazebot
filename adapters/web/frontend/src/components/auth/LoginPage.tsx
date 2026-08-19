// [2026-05-16] Login gate — admin token required before entering the app.
import { useEffect, useState } from 'react';

import { checkAdminAuth } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { Button } from '../common';
import { loadEntryNodes, TOKEN_SOURCE_HINT } from './loadEntryNodes';

export const LoginPage = () => {
  const { adminToken, setAdminToken, setAuthenticated } = useSettingsStore();
  const urlToken = new URLSearchParams(window.location.search).get('token') || '';
  const [tokenInput, setTokenInput] = useState(urlToken);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [autoVerifying, setAutoVerifying] = useState(!!adminToken);

  // Auto-verify saved token on mount. URL token is supported for server manager deep links.
  useEffect(() => {
    const tokenToVerify = urlToken || adminToken;
    if (!tokenToVerify) { setAutoVerifying(false); return; }
    checkAdminAuth(tokenToVerify).then(async ok => {
      if (ok) {
        setAdminToken(tokenToVerify);
        setAuthenticated(true);
        await loadEntryNodes(tokenToVerify);
      } else {
        if (!urlToken) setAdminToken(null);
      }
      setAutoVerifying(false);
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleLogin = async () => {
    setError('');
    const token = tokenInput.trim();
    if (!token) { setError('请输入令牌'); return; }
    setLoading(true);
    const ok = await checkAdminAuth(token);
    setLoading(false);
    if (ok) {
      setAdminToken(token);
      setAuthenticated(true);
      await loadEntryNodes(token);
    } else {
      setError(`令牌无效。${TOKEN_SOURCE_HINT}`);
    }
  };

  if (autoVerifying) {
    return (
      <div className="flex h-screen items-center justify-center bg-[var(--duties-bg)] text-[var(--duties-text)]">
        <div className="text-center">
          <p className="font-mono text-sm text-[var(--duties-tertiary)]">正在验证会话…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen items-center justify-center bg-[var(--duties-bg)] text-[var(--duties-text)]">
      <div className="w-full max-w-80 px-4">
        <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-6">
          <div className="flex items-center gap-3">
            <img src={`${import.meta.env.BASE_URL}logo-sm.jpg`} alt="Clonoth" className="h-11 w-11 rounded-lg" />
            <div>
              <h1 className="font-mono text-xl font-semibold tracking-[-0.04em]">Clonoth</h1>
              <p className="text-[0.6rem] text-[var(--duties-tertiary)]">调度器网页界面</p>
            </div>
          </div>
          <p className="mt-3 text-xs text-[var(--duties-secondary)]">请输入管理员令牌以继续</p>
          <input
            autoFocus
            className="mt-4 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
            onChange={e => setTokenInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleLogin()}
            placeholder="管理员令牌"
            type="password"
            value={tokenInput}
          />
          {error && <p className="mt-2 text-xs text-red-500">{error}</p>}
          <Button className="mt-4 w-full" disabled={loading} onClick={handleLogin} variant="primary">
            {loading ? '正在验证…' : '登录'}
          </Button>
        </div>
        <p className="mt-3 text-center font-mono text-[0.6rem] text-[var(--duties-tertiary)]">
          调度器网页界面
        </p>
      </div>
    </div>
  );
};
