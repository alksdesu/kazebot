// 控制台域的登录页。鉴权与对话界面共用同一个 token，只是视觉与文案按控制台重做。
import { useEffect, useState } from 'react';

import { checkAdminAuth } from '../api/supervisorClient';
import { loadEntryNodes, TOKEN_SOURCE_HINT } from '../components/auth/loadEntryNodes';
import { useSettingsStore } from '../store/settingsStore';
import './console.css';

export const ConsoleLogin = () => {
  const { adminToken, setAdminToken, setAuthenticated } = useSettingsStore();
  const urlToken = new URLSearchParams(window.location.search).get('token') || '';
  const [input, setInput] = useState(urlToken);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [verifying, setVerifying] = useState(!!(urlToken || adminToken));

  const accept = async (token: string) => {
    setAdminToken(token);
    setAuthenticated(true);
    await loadEntryNodes(token);
  };

  useEffect(() => {
    const candidate = urlToken || adminToken;
    if (!candidate) { setVerifying(false); return; }
    void checkAdminAuth(candidate).then(async (ok) => {
      if (ok) await accept(candidate);
      // URL 里带来的 token 不清 store：那是深链，清掉会让刷新后连保存的都没了。
      else if (!urlToken) setAdminToken(null);
      setVerifying(false);
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async () => {
    const token = input.trim();
    if (!token) { setError('请输入令牌'); return; }
    setError('');
    setBusy(true);
    const ok = await checkAdminAuth(token);
    setBusy(false);
    if (ok) await accept(token);
    else setError(`令牌无效。${TOKEN_SOURCE_HINT}`);
  };

  return (
    <div data-surface="console">
      <div className="qc-login">
        <div className="qc-login-card">
          <h1 className="qc-login-title">QQ 控制台</h1>
          <p className="qc-login-sub">
            {verifying ? '正在验证已保存的令牌…' : '控制 bot 在哪些群说话、什么时候开口、能干什么。'}
          </p>
          <label className="qc-login-label" htmlFor="qc-token">管理令牌</label>
          <input
            autoFocus
            className="qc-login-inp"
            disabled={verifying}
            id="qc-token"
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => event.key === 'Enter' && void submit()}
            type="password"
            value={input}
          />
          {error && <p className="qc-login-error">{error}</p>}
          <button
            className="qc-btn qc-login-btn"
            disabled={busy || verifying}
            onClick={() => void submit()}
            type="button"
          >
            {busy ? '正在验证…' : '进入'}
          </button>
          <p className="qc-login-where">
            令牌在 <code>data/.admin_token</code>，或由 <code>CLONOTH_ADMIN_TOKEN</code> 环境变量指定。
            连续输错会按次数退避，等提示的秒数过去再试。
          </p>
        </div>
      </div>
    </div>
  );
};
