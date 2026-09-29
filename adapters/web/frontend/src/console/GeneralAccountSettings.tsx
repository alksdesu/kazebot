import type { ReactNode } from 'react';

import { QQ_NAPCAT } from '../platforms/registry';
import { usePlatformRegistryState } from '../platforms/usePlatformRegistry';
import { useSettingsStore } from '../store/settingsStore';
import { scopedKey } from '../store/storageKey';
import { AccountWorkspaceSlot } from './AccountWorkspace';
import { Empty } from './components';
import { useConsoleStore } from './consoleStore';
import { useLiveRefresh } from './liveRefresh';
import { PlatformProvider } from './platformContext';
import { Section, SectionNote } from './Section';
import { SectionAnchor, useSectionOpenState } from './sectionOpen';
import { AccountSummary } from './sectionSummaries';

const AccountSection = ({ children, summary }: { children: ReactNode; summary?: ReactNode }) => {
  const { isOpen, requested, toggle } = useSectionOpenState(scopedKey('console_sections_general'));
  const open = isOpen('account');
  return <SectionAnchor deepLinked={requested === 'account'} open={open}>
    <Section id="account" title="账号与连接" icon="account_circle" open={open}
      onToggle={() => toggle('account')} summary={summary}>
      {children}
    </Section>
  </SectionAnchor>;
};

const CurrentAccountSettings = ({ token }: { token: string }) => {
  const refresh = useConsoleStore(state => state.refresh);
  useLiveRefresh(refresh, token);
  return <AccountSection summary={<AccountSummary />}>
    <SectionNote>每个实例使用一个平台、一个账号。这里管理实例、共享配置和当前实例的账号。换号会重启 NapCat，期间 bot 不可用。</SectionNote>
    <AccountWorkspaceSlot />
  </AccountSection>;
};

export const GeneralAccountSettings = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const registry = usePlatformRegistryState();
  if (!adminToken || !isAuthenticated) return null;
  if (!registry.settled) return <AccountSection><Empty>正在读取当前实例的平台…</Empty></AccountSection>;

  // 旧后端未给 current 时，只有唯一的平台才能确定账号请求的归属。
  const platform = registry.current
    ? registry.platforms.find(row => row.id === registry.current)
    : registry.platforms.length === 1 ? registry.platforms[0] : undefined;
  if (!platform) return <AccountSection><Empty>无法确定当前实例的平台，请刷新页面后重试。</Empty></AccountSection>;
  if (platform.id !== QQ_NAPCAT.id) return <AccountSection><Empty>当前实例的平台尚未提供账号管理界面。</Empty></AccountSection>;

  return <PlatformProvider key={`${adminToken}:${platform.id}`} platform={platform}>
    <CurrentAccountSettings token={adminToken} />
  </PlatformProvider>;
};
