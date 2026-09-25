import { useCallback, useEffect, useRef, useState } from 'react';

import { activeProviderConfig, checkHealth, getProviders } from '../../../api/supervisorClient';
import { confirmNavigation } from '../../../hooks/useUnsavedChanges';
import { useSettingsStore } from '../../../store/settingsStore';
import { LoginPage } from '../../auth/LoginPage';
import { Button } from '../../common';
import { Card, PageHeader, PageShell } from './settingsPagePrimitives';

export const GeneralSettingsPage = () => {
  const { adminToken, isAuthenticated, isConnected, storageWarning, setConnected, setModelConfig } = useSettingsStore();
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState('');
  const mounted = useRef(false);
  const healthRequest = useRef<AbortController | null>(null);
  const healthTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refreshHealth = useCallback(async () => {
    if (healthRequest.current) return;
    const controller = new AbortController();
    healthRequest.current = controller;
    const timeout = setTimeout(() => controller.abort(), 10000);
    healthTimeout.current = timeout;
    setChecking(true); setError('');
    try {
      await checkHealth(controller.signal);
      if (mounted.current && healthRequest.current === controller) setConnected(true);
    } catch {
      if (mounted.current && healthRequest.current === controller) {
        setConnected(false);
        setError('无法连接调度器。请检查服务是否运行，再刷新连接状态。');
      }
    } finally {
      clearTimeout(timeout);
      if (healthTimeout.current === timeout) healthTimeout.current = null;
      if (healthRequest.current === controller) {
        healthRequest.current = null;
        if (mounted.current) setChecking(false);
      }
    }
  }, [setConnected]);

  useEffect(() => {
    mounted.current = true;
    void refreshHealth();
    return () => {
      mounted.current = false; healthRequest.current?.abort(); healthRequest.current = null;
      if (healthTimeout.current) clearTimeout(healthTimeout.current);
      healthTimeout.current = null;
    };
  }, [refreshHealth]);

  useEffect(() => {
    if (!adminToken || !isAuthenticated) return;
    let active = true;
    void getProviders(adminToken).then(providers => {
      const current = useSettingsStore.getState();
      if (active && current.isAuthenticated && current.adminToken === adminToken) setModelConfig(activeProviderConfig(providers));
    }).catch(() => {});
    return () => { active = false; };
  }, [adminToken, isAuthenticated, setModelConfig]);

  const handleLogout = () => {
    if (!confirmNavigation()) return;
    const settings = useSettingsStore.getState();
    settings.setAdminToken(null);
    settings.setAuthenticated(false);
    settings.setAvailableNodes([]);
    settings.setModelConfig(null);
    settings.setActiveNode('', false, '');
    settings.setSessionProviderOverride(null);
    settings.setGlobalConfig('', '');
  };

  return <PageShell>
    <PageHeader title="通用" description="查看控制台连接，管理当前浏览器的管理员登录。退出登录不会停止机器人或后台任务。" />
    <Card title="调度器连接">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p role="status" className="flex items-center gap-2 text-sm"><span aria-hidden="true" className={`h-2 w-2 rounded-full ${isConnected ? 'bg-[var(--duties-live)]' : 'bg-[var(--duties-danger)]'}`} />{checking ? '正在检查连接…' : isConnected ? '调度器已连接' : '调度器未连接'}</p>
        <Button loading={checking} onClick={() => void refreshHealth()}>刷新连接</Button>
      </div>
      {error && <p role="alert" className="mt-3 text-sm text-[var(--duties-danger)]">{error}</p>}
      <p className="mt-3 text-sm text-[var(--duties-secondary)]">此状态表示控制台与调度器的连接。QQ 适配器和模型工作进程的状态请在“链路”和“运行”页面查看。</p>
    </Card>
    <Card title="管理员访问">
      {isAuthenticated ? <div className="space-y-4">
        <p className="text-sm">已认证。令牌不会在此页面显示。</p>
        <Button onClick={handleLogout}>退出登录</Button>
      </div> : <LoginPage embedded />}
      {storageWarning && <p role="status" className="mt-3 text-sm text-[var(--duties-secondary)]">{storageWarning}</p>}
    </Card>
  </PageShell>;
};
