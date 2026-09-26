// 控制台各页的控件出口。通用件出自 settingsControls，这里只加两件页壳：
// 平台页的生效状态条，以及不碰平台配置的页所用的素壳。
import { useEffect, type ReactNode } from 'react';

import { Button, Empty, ErrorText, PageShell, StatusBar, type PipTone } from '../components/settings/pages/settingsControls';
import { QQ_NAPCAT } from '../platforms/registry';
import { useSettingsStore } from '../store/settingsStore';
import { useConsoleStore } from './consoleStore';
import { PlatformProvider } from './platformContext';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';

export {
  Block,
  Button,
  Check,
  Chip,
  Chips,
  DangerPanel,
  Desc,
  Empty,
  ErrorText,
  Facts,
  Field,
  Fixed,
  Footnote,
  Grid,
  Input,
  Item,
  ItemTitle,
  LinkButton,
  List,
  Loose,
  Option,
  Pager,
  Panel,
  Pip,
  Pre,
  ReadOnlyRow,
  Row,
  SaveBar,
  Segmented,
  Select,
  Sub,
  Tag,
  Textarea,
} from '../components/settings/pages/settingsControls';

interface BarState {
  tone: PipTone;
  text: string;
}

/** pip 反映 bot 那边的生效状态，「N 项待应用」反映浏览器里还没应用的改动 —— 两个不同维度。 */
function barState(
  live: { published: boolean; stale?: boolean; applied?: boolean } | null,
  parseError: string,
): BarState {
  if (parseError) return { tone: 'halt', text: '配置有语法错误，bot 仍在用上一份' };
  if (live?.published && live.stale) return { tone: 'halt', text: 'bot 心跳已中断' };
  if (live?.published && live.applied) return { tone: 'live', text: '已生效' };
  if (live?.published) return { tone: 'halt', text: 'bot 用的还是旧配置' };
  return { tone: 'idle', text: '等待 bot 上报' };
}

const QqStatusBar = ({ confirmDiscard }: { confirmDiscard: () => boolean }) => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const { live, saving, draft, apply, discard } = useConsoleStore();
  const pending = Object.keys(draft).length;
  const { tone, text } = barState(live, String(live?.state?.parse_error || ''));

  return (
    <StatusBar tone={tone}>
      <span className="font-medium">{text}</span>
      {pending > 0 && (
        <>
          <span className="text-[var(--duties-border)]">·</span>
          <span className="font-medium text-[var(--duties-danger)]">{pending} 项待应用</span>
        </>
      )}
      <span className="flex-1" />
      <Button disabled={pending === 0 || saving} onClick={() => { if (confirmDiscard()) discard(); }} tone="quiet">丢弃</Button>
      <Button disabled={pending === 0 || saving} onClick={() => adminToken && void apply(adminToken)}>
        {saving ? '正在应用…' : '应用'}
      </Button>
    </StatusBar>
  );
};

/** 不碰平台配置的页面：只有标题、说明和登录提示，不去拉任何平台的生效状态。 */
export const PlainPage = ({ children, eyebrow, note, title }: {
  children: ReactNode;
  eyebrow: string;
  note: string;
  title: string;
}) => {
  const isAuthenticated = useSettingsStore((state) => state.isAuthenticated);
  return (
    <PageShell authed={isAuthenticated} description={note} eyebrow={eyebrow} title={title}>
      {children}
    </PageShell>
  );
};

interface QqPageProps {
  children: ReactNode;
  alwaysContent?: ReactNode;
  /** 写的不是 qq.yaml 的分区。bot 没跑也该能改，所以不等它公布生效配置。 */
  standalone?: boolean;
  note: string;
  title: string;
}

/** QQ 平台分区的共同外壳：在统一页壳上加生效状态条、解析错误提示和「bot 还没上报」的空态。 */
export const QqPage = ({ children, alwaysContent, standalone = false, note, title }: QqPageProps) => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const { live, loading, error, notice, refresh, draft } = useConsoleStore();
  const confirmDiscard = useUnsavedChanges(Object.keys(draft).length > 0, 'QQ 配置还有未应用修改，确定离开或丢弃吗？');
  const parseError = String(live?.state?.parse_error || '');

  // 每页各自拉一次。以前由控制台外壳统一拉，拆进设置页之后没有那个共同父级了。
  useEffect(() => {
    if (adminToken) void refresh(adminToken);
  }, [adminToken, refresh]);

  const body = standalone ? children
    : loading && !live ? <Empty>正在读取配置…</Empty>
      : !live?.published ? (
        <Empty>
          {live?.reason || 'bot 还没上报生效配置。在服务器上启动 QQ 适配器进程（bot.py）之后，这里会显示它实际在用的值。'}
        </Empty>
      ) : children;

  return (
    <PageShell
      authed={isAuthenticated}
      bar={isAuthenticated && !standalone ? <QqStatusBar confirmDiscard={confirmDiscard} /> : undefined}
      description={note}
      error={error}
      eyebrow={QQ_NAPCAT.displayName}
      notice={notice}
      title={title}
    >
      {parseError && (
        <ErrorText>
          qq.yaml 解析失败：{parseError}。bot 仍在用上一份有效配置。请先修复原文件语法；本页不会覆盖损坏文件。
        </ErrorText>
      )}
      <PlatformProvider platform={QQ_NAPCAT}>
        {alwaysContent}
        {body}
      </PlatformProvider>
    </PageShell>
  );
};

export { barState };
