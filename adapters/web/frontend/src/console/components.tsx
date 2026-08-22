// QQ 域各页的控件出口。通用件出自 settingsControls，这里只加该域独有的两件：
// 顶部那条 qq.yaml 生效状态条，和把它与加载/未发布态串起来的页壳。
import { useEffect, type ReactNode } from 'react';

import {
  Button,
  Empty,
  ErrorText,
  Facts,
  StatusBar,
  type PipTone,
} from '../components/settings/pages/settingsControls';
import { AuthRequired, PageHeader } from '../components/settings/pages/settingsPagePrimitives';
import { useSettingsStore } from '../store/settingsStore';
import { useConsoleStore } from './consoleStore';

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
  Footnote,
  Grid,
  Input,
  Item,
  ItemTitle,
  LinkButton,
  List,
  Loose,
  Option,
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

/** pip 反映 bot 那边的生效状态，「N 项待应用」反映浏览器里还没提交的改动 —— 两个不同维度。 */
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

const QqStatusBar = () => {
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
      <Button disabled={pending === 0 || saving} onClick={discard} tone="quiet">丢弃</Button>
      <Button disabled={pending === 0 || saving} onClick={() => adminToken && void apply(adminToken)}>
        {saving ? '正在应用…' : '应用'}
      </Button>
    </StatusBar>
  );
};

interface QqPageProps {
  children: ReactNode;
  /** 写的不是 qq.yaml 的页面。bot 没跑也该能改，所以不等它公布生效配置。 */
  standalone?: boolean;
  note: string;
  title: string;
}

/** QQ 域页面的共同外壳：生效状态条、错误提示，以及「bot 还没上报」时的空态。
 *
 * 状态条不进滚动区：改完页尾的开关要能直接点应用，滚回顶部才找得到等于没有。 */
export const QqPage = ({ children, standalone = false, note, title }: QqPageProps) => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const { live, loading, error, notice, refresh } = useConsoleStore();
  const parseError = String(live?.state?.parse_error || '');

  // 每页各自拉一次。以前由控制台外壳统一拉，拆进设置页之后没有那个共同父级了。
  useEffect(() => {
    if (adminToken) void refresh(adminToken);
  }, [adminToken, refresh]);

  const body = standalone ? children
    : loading && !live ? <Empty>正在读取配置…</Empty>
      : !live?.published ? (
        <Empty>
          {live?.reason || 'bot 进程还没公布生效配置。启动 bot 之后这里会显示它实际在用的值。'}
        </Empty>
      ) : children;

  return (
    <section className="flex h-full min-h-0 flex-col">
      {isAuthenticated && <QqStatusBar />}
      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        {/* 基准字号。控制台自带的那条 13px 随 console.css 一起没了，不接住的话
            所有靠继承的文本会跳到浏览器默认的 16px，比旁边显式写死的小字大一整档。 */}
        <div className="mx-auto max-w-4xl space-y-5 text-sm">
          <PageHeader description={note} eyebrow="QQ 机器人" title={title} />
          {!isAuthenticated ? <AuthRequired /> : (
            <>
              {error && <ErrorText>{error}</ErrorText>}
              {notice && <Facts>{notice}</Facts>}
              {parseError && (
                <ErrorText>
                  qq.yaml 解析失败：{parseError}。bot 仍在用上一份有效配置，改完应用即可恢复。
                </ErrorText>
              )}
              {body}
            </>
          )}
        </div>
      </div>
    </section>
  );
};

export { barState };
