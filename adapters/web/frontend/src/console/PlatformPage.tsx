// 平台页：固定状态栏 + 连接卡 + 折叠分区。分区展开状态只存在这台浏览器里。
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

import {
  Button,
  Empty,
  ErrorText,
  PageShell,
  StatusBar,
  type PipTone,
} from '../components/settings/pages/settingsControls';
import { useSettingsStore } from '../store/settingsStore';
import { scopedKey } from '../store/storageKey';
import { useViewStore } from '../store/viewStore';
import type { PlatformDescriptor } from '../platforms/registry';
import { useConsoleStore, useSectionPending } from './consoleStore';
import { PlatformProvider } from './platformContext';
import { Section } from './Section';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';

export interface PlatformSection {
  /** 分区 id，同时是 ?section= 的取值和记住展开状态的键。 */
  id: string;
  label: string;
  icon: string;
  /** 这一块讲的是什么。展开后摆在分区正文顶上。 */
  note: string;
  /** 归这个分区管的配置段，对着 bot 公布的 paths 比。空表示不写平台配置。 */
  paths: readonly string[];
  Summary: () => JSX.Element;
  Body: () => JSX.Element;
}

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

const PlatformStatusBar = ({ confirmDiscard }: { confirmDiscard: () => boolean }) => {
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

type OpenMap = Record<string, boolean>;

function readOpenMap(storeKey: string): OpenMap {
  try {
    const parsed = JSON.parse(localStorage.getItem(storeKey) || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(([, open]) => typeof open === 'boolean'),
    ) as OpenMap;
  } catch {
    // 存坏了就按默认展开状态来，不该把整页拖垮。
    return {};
  }
}

function writeOpenMap(storeKey: string, value: OpenMap): void {
  try {
    localStorage.setItem(storeKey, JSON.stringify(value));
  } catch {
    // 隐私模式下 localStorage 会抛，记不住展开状态不影响配置本身。
  }
}

const SectionEntry = ({ deepLinked, onToggle, open, section }: {
  deepLinked: boolean;
  onToggle: (id: string) => void;
  open: boolean;
  section: PlatformSection;
}) => {
  const pending = useSectionPending(section.paths);
  const anchor = useRef<HTMLDivElement>(null);

  // 深链进来要滚到这一块。等展开渲染完再滚，否则量到的是收起时的位置。
  useEffect(() => {
    if (!deepLinked || !open) return;
    // 可选调用：jsdom 和一些内嵌浏览器没有这个方法，缺它不该把整页打挂。
    anchor.current?.scrollIntoView?.({ block: 'start' });
  }, [deepLinked, open]);

  return (
    <div ref={anchor}>
      <Section
        icon={section.icon}
        id={section.id}
        onToggle={() => onToggle(section.id)}
        open={open}
        pending={pending}
        summary={<section.Summary />}
        title={section.label}
      >
        <p className="mb-4 max-w-[var(--duties-prose-max-width)] text-xs leading-5 text-[var(--duties-secondary)]">
          {section.note}
        </p>
        <section.Body />
      </Section>
    </div>
  );
};

interface PlatformPageProps {
  /** 状态栏之下、分区之上那一张卡。连接是每个平台都有的一件事，不进折叠。 */
  connection: ReactNode;
  note: string;
  platform: PlatformDescriptor;
  sections: readonly PlatformSection[];
}

export const PlatformPage = ({ connection, note, platform, sections }: PlatformPageProps) => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const { live, loading, error, notice, refresh, draft } = useConsoleStore();
  const requestedSection = useViewStore((state) => state.activeSettingsSection);
  const confirmDiscard = useUnsavedChanges(
    Object.keys(draft).length > 0,
    `${platform.displayName} 配置还有未应用修改，确定离开或丢弃吗？`,
  );
  const parseError = String(live?.state?.parse_error || '');

  const storeKey = scopedKey(`console_sections_${platform.id}`);
  const [openMap, setOpenMap] = useState<OpenMap>(() => readOpenMap(storeKey));

  const toggle = useCallback((id: string) => {
    setOpenMap((current) => {
      const next = { ...current, [id]: !current[id] };
      writeOpenMap(storeKey, next);
      return next;
    });
  }, [storeKey]);

  // 深链指名的那一块无条件展开，记住的收起状态不该挡住它。
  useEffect(() => {
    if (!requestedSection) return;
    setOpenMap((current) => (current[requestedSection] ? current : { ...current, [requestedSection]: true }));
  }, [requestedSection]);

  // 各设置页之间没有共同的父组件替它们拉，只能每页各自拉一次。
  useEffect(() => {
    if (adminToken) void refresh(adminToken);
  }, [adminToken, refresh]);

  const published = Boolean(live?.published);
  const body = loading && !live ? <Empty>正在读取配置…</Empty>
    : !published ? (
      <Empty>
        {live?.reason || `bot 还没上报生效配置。在服务器上启动 ${platform.displayName} 的适配器进程（bot.py）之后，这里会显示它实际在用的值。`}
      </Empty>
    ) : null;

  return (
    <PageShell
      authed={isAuthenticated}
      bar={isAuthenticated ? <PlatformStatusBar confirmDiscard={confirmDiscard} /> : undefined}
      description={note}
      error={error}
      eyebrow="平台"
      notice={notice}
      title={platform.displayName}
    >
      {parseError && (
        <ErrorText>
          平台配置解析失败：{parseError}。bot 仍在用上一份有效配置。请先修复原文件语法；本页不会覆盖损坏文件。
        </ErrorText>
      )}
      <PlatformProvider platform={platform}>
        {connection}
        {body}
        <div className="border-t border-[var(--duties-border)]">
          {sections.map((section) => (
            // 不写平台配置的分区（账号与连接）在 bot 没上报时照样能用。
            (published || !section.paths.length) && (
              <SectionEntry
                deepLinked={requestedSection === section.id}
                key={section.id}
                onToggle={toggle}
                open={Boolean(openMap[section.id])}
                section={section}
              />
            )
          ))}
        </div>
      </PlatformProvider>
    </PageShell>
  );
};

export { barState, PlatformStatusBar };
