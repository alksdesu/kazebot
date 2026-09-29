import { QQ_NAPCAT } from '../platforms/registry';
import { usePlatformRegistryState } from '../platforms/usePlatformRegistry';
import { useSettingsStore } from '../store/settingsStore';
import { AccountWorkspaceSlot } from './AccountWorkspace';
import { Empty } from './components';
import { useConsoleStore } from './consoleStore';
import { useLiveRefresh } from './liveRefresh';
import { PlatformProvider } from './platformContext';
import { SectionNote } from './Section';
import { AccountSummary } from './sectionSummaries';

const CurrentAccountSettings = ({ token }: { token: string }) => {
  const refresh = useConsoleStore(state => state.refresh);
  useLiveRefresh(refresh, token);
  return <>
    <p role="status" className="mb-4 text-sm text-[var(--duties-secondary)]"><AccountSummary /></p>
    <SectionNote>换号会重启 NapCat，期间 bot 不可用。</SectionNote>
    <AccountWorkspaceSlot />
  </>;
};

export const AccountSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const registry = usePlatformRegistryState();
  if (!adminToken || !isAuthenticated) return <Empty>需要管理员令牌。</Empty>;
  if (!registry.settled) return <Empty>正在读取当前实例的平台…</Empty>;

  // 旧后端未给 current 时，只有唯一的平台才能确定账号请求的归属。
  const platform = registry.current
    ? registry.platforms.find(row => row.id === registry.current)
    : registry.platforms.length === 1 ? registry.platforms[0] : undefined;
  if (!platform) return <Empty>无法确定当前实例的平台，请刷新页面后重试。</Empty>;
  if (platform.id !== QQ_NAPCAT.id) return <Empty>当前实例的平台尚未提供账号管理界面。</Empty>;

  return <PlatformProvider key={`${adminToken}:${platform.id}`} platform={platform}>
    <CurrentAccountSettings token={adminToken} />
  </PlatformProvider>;
};
