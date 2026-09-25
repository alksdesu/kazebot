import { useCallback, useEffect, useRef, useState } from 'react';

import { verifyAdminAuth } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { Button } from '../common';
import { loadEntryNodes, TOKEN_SOURCE_HINT } from './loadEntryNodes';

type VerificationMode = 'saved' | 'link' | 'manual';
const AUTH_TIMEOUT_MS = 10000;

export const LoginPage = ({ embedded = false }: { embedded?: boolean }) => {
  const { adminToken, setAdminToken, setAuthenticated } = useSettingsStore();
  const [urlToken] = useState(() => new URLSearchParams(window.location.search).get('token') || '');
  const initialToken = useRef(urlToken || adminToken);
  const inputRef = useRef<HTMLInputElement>(null);
  const focusInput = useRef(false);
  const sequence = useRef(0);
  const request = useRef<{ controller: AbortController; timer: ReturnType<typeof setTimeout> } | null>(null);
  const [tokenInput, setTokenInput] = useState(urlToken);
  const [error, setError] = useState('');
  const [verifying, setVerifying] = useState<VerificationMode | null>(null);

  const verify = useCallback(async (value: string, mode: VerificationMode) => {
    if (request.current) return;
    const token = value.trim();
    if (!token) { setError('请输入管理员令牌。'); inputRef.current?.focus(); return; }
    const id = ++sequence.current;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      if (sequence.current !== id) return;
      controller.abort();
      request.current = null;
      sequence.current++;
      setVerifying(null);
      setError('连接超时。请检查服务是否运行，然后重新登录；已保存的令牌未删除。');
    }, AUTH_TIMEOUT_MS);
    request.current = { controller, timer };
    setError('');
    setVerifying(mode);
    try {
      const ok = await verifyAdminAuth(token, controller.signal);
      if (sequence.current !== id || controller.signal.aborted) return;
      if (!ok) {
        if (mode !== 'manual' && useSettingsStore.getState().adminToken === token) setAdminToken(null);
        setError('令牌无效或已过期，请检查后重试。');
        return;
      }
      setAdminToken(token);
      setAuthenticated(true);
      void loadEntryNodes(token, () => {
        const current = useSettingsStore.getState();
        return current.isAuthenticated && current.adminToken === token;
      });
    } catch {
      if (sequence.current === id && !controller.signal.aborted) {
        setError('暂时无法验证登录。请检查服务和网络后重试；已保存的令牌未删除。');
      }
    } finally {
      clearTimeout(timer);
      if (sequence.current === id) { request.current = null; setVerifying(null); }
    }
  }, [setAdminToken, setAuthenticated]);

  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.has('token')) {
      url.searchParams.delete('token');
      window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
    }
    if (initialToken.current) void verify(initialToken.current, urlToken ? 'link' : 'saved');
    return () => {
      sequence.current++;
      if (request.current) { request.current.controller.abort(); clearTimeout(request.current.timer); request.current = null; }
    };
  }, [urlToken, verify]);

  useEffect(() => {
    if (!verifying && focusInput.current) { inputRef.current?.focus(); focusInput.current = false; }
  }, [verifying]);

  const useAnotherToken = () => {
    sequence.current++;
    if (request.current) { request.current.controller.abort(); clearTimeout(request.current.timer); request.current = null; }
    focusInput.current = true;
    setTokenInput('');
    setError('');
    setVerifying(null);
  };

  const Container = embedded ? 'div' : 'main';
  return (
    <Container className={embedded ? 'text-[var(--duties-text)]' : 'flex min-h-dvh items-center justify-center bg-[var(--duties-bg)] px-4 py-8 text-[var(--duties-text)]'}>
      <section className={embedded ? 'w-full max-w-md' : 'w-full max-w-md border border-[var(--duties-border)] bg-[var(--duties-surface)] p-6 sm:p-8'} aria-labelledby={embedded ? undefined : 'login-title'}>
        {!embedded && <header className="flex items-center gap-3">
          <img src={`${import.meta.env.BASE_URL}logo-sm.jpg`} alt="" className="h-11 w-11 rounded-lg" />
          <div><h1 id="login-title" className="text-xl font-semibold">Clonoth 控制台</h1><p className="mt-1 text-sm text-[var(--duties-secondary)]">管理对话、任务与机器人</p></div>
        </header>}
        <form aria-label="管理员登录" className={embedded ? 'space-y-4' : 'mt-6 space-y-4'} onSubmit={event => { event.preventDefault(); void verify(tokenInput, 'manual'); }}>
          <div>
            <label htmlFor="admin-token" className="mb-2 block text-sm font-medium">管理员令牌</label>
            <input
              ref={inputRef} id="admin-token" autoFocus disabled={Boolean(verifying)}
              autoComplete="current-password" spellCheck={false}
              className="app-input w-full text-base"
              onChange={event => setTokenInput(event.target.value)}
              onKeyDown={event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); void verify(tokenInput, 'manual'); } }}
              placeholder="管理员令牌" type="password" value={tokenInput}
              aria-describedby={error ? 'login-help login-error' : 'login-help'} aria-invalid={Boolean(error)}
            />
          </div>
          {error && <p id="login-error" role="alert" className="text-sm text-[var(--duties-danger)]">{error}</p>}
          {verifying && <p role="status" className="text-sm text-[var(--duties-secondary)]">{verifying === 'manual' ? '正在验证令牌…' : '正在恢复已保存的登录…'}</p>}
          <Button className="w-full" disabled={Boolean(verifying)} type="submit" variant="primary">{verifying ? '正在验证…' : '登录'}</Button>
          {!verifying && error && adminToken && !tokenInput && <Button className="w-full" onClick={() => void verify(adminToken, 'saved')} type="button">重试已保存登录</Button>}
          {verifying && <Button className="w-full" onClick={useAnotherToken} type="button">使用其他令牌</Button>}
        </form>
        <p id="login-help" className="mt-5 text-sm leading-relaxed text-[var(--duties-secondary)]">{TOKEN_SOURCE_HINT}</p>
      </section>
    </Container>
  );
};
