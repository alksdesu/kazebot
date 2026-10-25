import { useCallback, useEffect, useRef, useState } from 'react';

import type { RequestTicket } from '../../../api/requestGuard';
import { activeProviderConfig, checkHealth, getProviders, HEALTH_TIMEOUT_MS } from '../../../api/supervisorClient';
import { useRequestGuard } from '../../../hooks/useRequestGuard';
import { confirmNavigation } from '../../../hooks/useUnsavedChanges';
import { useChatStore } from '../../../store/chatStore';
import { useSettingsStore } from '../../../store/settingsStore';
import { LoginPage } from '../../auth/LoginPage';
import { Block, Button, PageShell } from './settingsControls';

export const GeneralSettingsPage = () => {
  const { adminToken, isAuthenticated, isConnected, storageWarning, setConnected, setModelConfig } = useSettingsStore();
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState('');
  // 健康检查是公开端点，换登录不作废；模型配置跟着登录走。
  const health = useRequestGuard('', { anonymous: true });
  const requests = useRequestGuard();
  const healthCheck = useRef<RequestTicket | null>(null);

  const refreshHealth = useCallback(async () => {
    if (healthCheck.current?.isCurrent()) return;
    const request = health.begin('health');
    healthCheck.current = request;
    setChecking(true); setError('');
    try {
      await checkHealth(request.signal, HEALTH_TIMEOUT_MS);
      if (request.isCurrent()) setConnected(true);
    } catch {
      if (request.isCurrent()) {
        setConnected(false);
        setError('无法连接调度器。请检查服务是否运行，再刷新连接状态。');
      }
    } finally {
      if (healthCheck.current === request) {
        healthCheck.current = null;
        if (request.isCurrent()) setChecking(false);
      }
    }
  }, [health, setConnected]);

  useEffect(() => { void refreshHealth(); }, [refreshHealth]);

  useEffect(() => {
    if (!adminToken || !isAuthenticated) return;
    const request = requests.begin('providers');
    void getProviders(adminToken).then(providers => {
      if (request.isCurrent()) setModelConfig(activeProviderConfig(providers));
    }).catch(() => {});
  }, [requests, adminToken, isAuthenticated, setModelConfig]);

  const handleLogout = () => {
    if (!confirmNavigation()) return;
    // 先关掉全局事件流、清掉这次登录留在内存里的对话，再丢令牌。
    useChatStore.getState().resetState();
    const settings = useSettingsStore.getState();
    settings.setAdminToken(null);
    settings.setAuthenticated(false);
    settings.setAvailableNodes([]);
    settings.setModelConfig(null);
    settings.setActiveNode('', false, '');
    settings.setSessionProviderOverride(null);
    settings.setGlobalConfig('', '');
  };

  return <PageShell title="通用" description="查看控制台连接，管理当前浏览器的管理员登录。退出登录不会停止机器人或后台任务。">
    <Block frame="boxed" title="调度器连接">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p role="status" className="flex items-center gap-2 text-sm"><span aria-hidden="true" className={`h-2 w-2 rounded-full ${isConnected ? 'bg-[var(--duties-live)]' : 'bg-[var(--duties-danger)]'}`} />{checking ? '正在检查连接…' : isConnected ? '调度器已连接' : '调度器未连接'}</p>
        <Button tone="quiet" loading={checking} onClick={() => void refreshHealth()}>刷新连接</Button>
      </div>
      {error && <p role="alert" className="mt-3 text-sm text-[var(--duties-danger)]">{error}</p>}
      <p className="mt-3 text-sm text-[var(--duties-secondary)]">此状态表示控制台与调度器的连接。平台适配进程的状态在该平台的「链路」页，模型工作进程的状态在引擎的「运行」页。</p>
    </Block>
    <Block frame="boxed" title="管理员访问">
      {isAuthenticated ? <div className="space-y-4">
        <p className="text-sm">已认证。令牌不会在此页面显示。</p>
        <Button tone="quiet" onClick={handleLogout}>退出登录</Button>
      </div> : <LoginPage embedded />}
      {storageWarning && <p role="status" className="mt-3 text-sm text-[var(--duties-secondary)]">{storageWarning}</p>}
    </Block>
  </PageShell>;
};
