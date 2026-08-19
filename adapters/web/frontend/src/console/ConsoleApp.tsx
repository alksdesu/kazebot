// QQ 控制台外壳。自带左窄轨与生效状态条，不走 AppLayout —— 那套是三栏聊天布局，
// sidebar/header 都是必填槽，塞进去就没法视觉独立。
import { useEffect } from 'react';

import { AccountPage } from './AccountPage';
import { ChannelsPage } from './ChannelsPage';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import { Empty, Page, Rail, StatusBar } from './components';
import './console.css';
import { DOMAIN_LABELS, useConsoleStore, type ConsoleDomain } from './consoleStore';
import { ModelsPage } from './ModelsPage';
import { PermissionsPage } from './PermissionsPage';
import { PersonaPage } from './PersonaPage';
import { ProvidersPage } from './ProvidersPage';
import { RuntimePage } from './RuntimePage';
import { TimingPage } from './TimingPage';

const READY: ReadonlySet<ConsoleDomain> = new Set<ConsoleDomain>([
  'account', 'channels', 'timing', 'permissions', 'persona', 'providers', 'models', 'runtime',
]);

// 这些页写的不是 qq.yaml，也就不需要等 bot 公布生效配置——bot 没跑也该能改。
const WITHOUT_BOT: ReadonlySet<ConsoleDomain> = new Set<ConsoleDomain>(['account', 'providers', 'models']);

const PAGES: Partial<Record<ConsoleDomain, () => JSX.Element>> = {
  account: AccountPage,
  channels: ChannelsPage,
  timing: TimingPage,
  permissions: PermissionsPage,
  providers: ProvidersPage,
  models: ModelsPage,
  persona: PersonaPage,
  runtime: RuntimePage,
};

const PAGE_NOTES: Partial<Record<ConsoleDomain, string>> = {
  account: 'bot 用哪个 QQ 号登录。换号会重启 NapCat，期间 bot 不可用；新号不在原来的群里的话，群名单和管理员名单都要重配。',
  channels: 'bot 只在名单里的群和私聊里出现。两份名单都是白名单，不在名单里的消息连处理都不会处理。',
  timing: '决定 bot 在群里什么时候说话。多个条件之间是「或」——命中任意一条就会回应，再由冷却决定是否真的开口。',
  permissions: '谁能对 bot 下管理命令。管理员权限跨所有群和私聊生效，不区分场景。',
  persona: 'bot 是谁、能做什么。上半页写的是节点文件，各自存盘；下半页走 qq.yaml，改完点顶部的应用。',
  providers: 'bot 用哪一家的模型，以及它的地址和密钥。改完下一条消息就生效，不用重启。',
  models: '发给模型的请求里带哪些参数。清单由各家 provider 自己声明，全局与节点分两层，节点优先。',
  runtime: '进程与链路的实况，以及几个改完立刻重建运行期对象的参数。',
};

export const ConsoleApp = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const closeSettings = useViewStore((state) => state.closeSettings);
  const {
    domain, live, loading, saving, error, notice, draft,
    setDomain, refresh, apply, discard,
  } = useConsoleStore();

  useEffect(() => {
    if (adminToken) void refresh(adminToken);
  }, [adminToken, refresh]);

  const pending = Object.keys(draft).length;
  const parseError = String(live?.state?.parse_error || '');
  const Body = PAGES[domain];

  return (
    <div data-surface="console">
      <div className="qc-shell">
        <Rail domain={domain} onExit={closeSettings} onSelect={setDomain} ready={READY} />
        <main>
          <StatusBar
            live={live}
            onApply={() => adminToken && void apply(adminToken)}
            onDiscard={discard}
            parseError={parseError}
            pending={pending}
            saving={saving}
          />
          <Page note={PAGE_NOTES[domain]} title={DOMAIN_LABELS[domain]}>
            {error && <p className="qc-login-error">{error}</p>}
            {notice && <p className="qc-footnote">{notice}</p>}
            {parseError && (
              <p className="qc-login-error">
                qq.yaml 解析失败：{parseError}。bot 仍在用上一份有效配置，改完应用即可恢复。
              </p>
            )}
            {WITHOUT_BOT.has(domain) && Body ? (
              <Body />
            ) : loading && !live ? (
              <Empty>正在读取配置…</Empty>
            ) : !live?.published ? (
              <Empty>
                {live?.reason || 'bot 进程还没公布生效配置。启动 bot 之后这里会显示它实际在用的值。'}
              </Empty>
            ) : Body ? (
              <Body />
            ) : (
              <Empty>这一页还没做。</Empty>
            )}
          </Page>
        </main>
      </div>
    </div>
  );
};
