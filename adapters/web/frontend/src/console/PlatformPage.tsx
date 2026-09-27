// 平台页：固定状态栏 + 连接卡 + 折叠分区。分区和字段来自后端 schema，展开状态只存在这台浏览器里。
import { useEffect, type ReactNode } from 'react';

import type { ConsoleSchema, ConsoleSectionSchema } from '../api/supervisorClient';
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
import type { PlatformDescriptor } from '../platforms/registry';
import { FEATURES_SCOPE, platformScope } from './configScope';
import { configStore, useConsoleStore, useSectionPending } from './consoleStore';
import { SchemaFields } from './FieldRenderer';
import { PlatformProvider } from './platformContext';
import { useConsoleSchema, useSchemaStore } from './schemaStore';
import { Section, SectionNote } from './Section';
import { SectionAnchor, useSectionOpenState } from './sectionOpen';
import { PartsProvider, useSectionParts, type PageParts } from './sectionParts';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';

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

const SectionEntry = ({ deepLinked, fields, onToggle, open, section }: {
  deepLinked: boolean;
  fields: ConsoleSchema['fields'];
  onToggle: (id: string) => void;
  open: boolean;
  section: ConsoleSectionSchema;
}) => {
  const pending = useSectionPending(section.paths);
  const Summary = useSectionParts(section.id)?.Summary;

  return (
    <SectionAnchor deepLinked={deepLinked} open={open}>
      <Section
        icon={section.icon}
        id={section.id}
        onToggle={() => onToggle(section.id)}
        open={open}
        pending={pending}
        summary={Summary ? <Summary /> : null}
        title={section.label}
      >
        <SectionNote>{section.note}</SectionNote>
        <SchemaFields fields={fields} nodes={section.nodes} sectionId={section.id} />
      </Section>
    </SectionAnchor>
  );
};

interface PlatformPageProps {
  /** 状态栏之下、分区之上那一张卡。连接是每个平台都有的一件事，不进折叠。 */
  connection: ReactNode;
  note: string;
  /** 分区里前端自己画的部分：摘要和编不成字段的块。 */
  parts: PageParts;
  platform: PlatformDescriptor;
}

const PlatformPageBody = ({ connection, note, platform }: Omit<PlatformPageProps, 'parts'>) => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const { live, loading, error, notice, refresh, draft } = useConsoleStore();
  const scope = platformScope(platform.id);
  const { schema, error: schemaError } = useConsoleSchema(scope);
  const loadSchema = useSchemaStore((state) => state.load);
  const refreshInherited = configStore(FEATURES_SCOPE)((state) => state.refresh);
  const confirmDiscard = useUnsavedChanges(
    Object.keys(draft).length > 0,
    `${platform.displayName} 配置还有未应用修改，确定离开或丢弃吗？`,
  );
  const parseError = String(live?.state?.parse_error || '');

  const { isOpen, requested, toggle } = useSectionOpenState(scopedKey(`console_sections_${platform.id}`));

  // 各设置页之间没有共同的父组件替它们拉，只能每页各自拉一次。
  // 全局那份也拉：共享键选「跟随全局」之后，没应用之前要显示全局那边的值。
  useEffect(() => {
    if (!adminToken) return;
    void refresh(adminToken);
    void refreshInherited(adminToken);
    void loadSchema(adminToken, scope);
  }, [adminToken, loadSchema, refresh, refreshInherited, scope]);

  const published = Boolean(live?.published);
  const body = loading && !live ? <Empty>正在读取配置…</Empty>
    : !published ? (
      <Empty>
        {live?.reason || `bot 还没上报生效配置。在服务器上启动 ${platform.displayName} 的适配器进程（bot.py）之后，这里会显示它实际在用的值。`}
      </Empty>
    ) : null;
  const layout = schemaError ? <ErrorText>{schemaError}</ErrorText>
    : !schema && adminToken ? <Empty>正在读取控制台布局…</Empty>
      : null;

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
      {connection}
      {body}
      {layout}
      {schema && (
        <div className="border-t border-[var(--duties-border)]">
          {schema.sections.map((section) => (
            // 不写平台配置的分区（账号与连接）在 bot 没上报时照样能用。
            (published || !section.paths.length) && (
              <SectionEntry
                deepLinked={requested === section.id}
                fields={schema.fields}
                key={section.id}
                onToggle={toggle}
                open={isOpen(section.id)}
                section={section}
              />
            )
          ))}
        </div>
      )}
    </PageShell>
  );
};

// 平台和配置作用域套在最外层：状态栏、应用按钮读的草稿必须和分区里写的是同一份。
export const PlatformPage = ({ parts, platform, ...rest }: PlatformPageProps) => (
  <PlatformProvider platform={platform}>
    <PartsProvider parts={parts}>
      <PlatformPageBody platform={platform} {...rest} />
    </PartsProvider>
  </PlatformProvider>
);

export { barState, PlatformStatusBar };
