// 机器人 › 全局默认：各平台没单独设置时用的值，写进全局设置文件。区块各自保存，没有「应用」。
import { useEffect, useMemo } from 'react';

import type { ConsoleSchema, ConsoleSectionSchema, FeatureAdoption, FeaturesLiveState } from '../api/supervisorClient';
import type { PipTone } from '../components/settings/pages/settingsControls';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';
import { scopedKey } from '../store/storageKey';
import { Block, Empty, ErrorText, ItemTitle, List, Pip, Row } from './components';
import { ConfigScopeProvider, FEATURES_SCOPE } from './configScope';
import { countDraftFor, useConsoleStore, useDraftCount } from './consoleStore';
import { DEFAULTS_PAGE_SECTIONS, FeatureSectionBody } from './FeatureSection';
import { useLiveRefresh } from './liveRefresh';
import { fieldKeys } from './schemaNodes';
import { useConsoleSchema, useSchemaStore } from './schemaStore';
import { Section, SectionNote } from './Section';
import { SectionAnchor, useSectionOpenState } from './sectionOpen';
import { picksSection } from './sectionPick';

/** 一个平台有没有换上当前这份全局文件。颜色之外文字自带状态，读屏也拿得到。 */
export function adoptionState(platform: FeatureAdoption): { tone: PipTone; text: string } {
  if (!platform.published) return { tone: 'idle', text: platform.reason || 'bot 没在跑' };
  if (platform.stale) return { tone: 'halt', text: 'bot 心跳已中断，不算在内' };
  if (platform.applied) return { tone: 'live', text: '已换上当前这份' };
  return { tone: 'halt', text: platform.reason || '还没换上当前这份' };
}

const Adoption = ({ platforms }: { platforms: readonly FeatureAdoption[] }) => (
  <Block hint="保存之后，在跑的 bot 两秒内换上新文件" title="各平台">
    {platforms.length ? (
      <List>
        {platforms.map((platform) => {
          const { tone, text } = adoptionState(platform);
          return (
            <Row key={platform.id}>
              <Pip tone={tone} />
              <ItemTitle>{platform.display_name}</ItemTitle>
              <span className="text-xs text-[var(--duties-secondary)]">{text}</span>
            </Row>
          );
        })}
      </List>
    ) : (
      <Empty>还没有能读全局设置的平台。</Empty>
    )}
  </Block>
);

const FeatureSectionEntry = ({ deepLinked, fields, onToggle, open, section }: {
  deepLinked: boolean;
  fields: ConsoleSchema['fields'];
  onToggle: (id: string) => void;
  open: boolean;
  section: ConsoleSectionSchema;
}) => {
  const names = useMemo(() => fieldKeys(section.nodes), [section]);
  const pending = useDraftCount(names);
  return (
    <SectionAnchor deepLinked={deepLinked} open={open}>
      <Section
        commitVerb="保存"
        icon={section.icon}
        id={section.id}
        onToggle={() => onToggle(section.id)}
        open={open}
        pending={pending}
        summary={null}
        title={section.label}
      >
        <SectionNote>{section.note}</SectionNote>
        <FeatureSectionBody fields={fields} names={names} section={section} />
      </Section>
    </SectionAnchor>
  );
};

const FeaturesPageBody = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const { loadError, live, loading, refresh } = useConsoleStore();
  const { schema, error: schemaError } = useConsoleSchema(FEATURES_SCOPE);
  const loadSchema = useSchemaStore((state) => state.load);
  const { isOpen, requested, toggle } = useSectionOpenState(scopedKey('console_sections_features'));

  const sections = useMemo(
    () => (schema?.sections || []).filter((section) => picksSection(DEFAULTS_PAGE_SECTIONS, section.id)),
    [schema],
  );
  // 只算这一页摆出来的键：图库那几项在表情包页上改，没保存也不该拦着离开这一页。
  const sectionKeys = useMemo(
    () => sections.map((section) => ({ label: section.label, names: fieldKeys(section.nodes) })),
    [sections],
  );
  // 各块分开保存，离开时得说清是哪几块没存；拼成字符串再比，选择器才不会每次给新值。
  const unsaved = useConsoleStore((state) => sectionKeys
    .filter(({ names }) => countDraftFor(state, names) > 0)
    .map(({ label }) => `「${label}」`)
    .join(''));
  useUnsavedChanges(Boolean(unsaved), `全局默认的${unsaved}还有没保存的修改，确定离开吗？`);

  useLiveRefresh(refresh, adminToken);
  useEffect(() => {
    if (!adminToken) return;
    void loadSchema(adminToken, FEATURES_SCOPE);
  }, [adminToken, loadSchema]);

  const parseError = String(live?.state?.parse_error || '');
  const platforms = (live as FeaturesLiveState | null)?.platforms || [];

  return (
    <>
      {loadError && <ErrorText role="alert">{loadError}</ErrorText>}
      {parseError && (
        <ErrorText role="alert">
          全局设置解析失败：{parseError}。bot 仍在用上一份有效内容。请先修复原文件语法；本页不会覆盖损坏文件。
        </ErrorText>
      )}
      {loading && !live ? <Empty>正在读取全局设置…</Empty> : live && <Adoption platforms={platforms} />}
      {schemaError ? <ErrorText>{schemaError}</ErrorText>
        : !schema ? adminToken && <Empty>正在读取控制台布局…</Empty>
          : (
            <div className="border-t border-[var(--duties-border)]">
              {sections.map((section) => (
                <FeatureSectionEntry
                  deepLinked={requested === section.id}
                  fields={schema.fields}
                  key={section.id}
                  onToggle={toggle}
                  open={isOpen(section.id)}
                  section={section}
                />
              ))}
            </div>
          )}
    </>
  );
};

export const FeaturesPage = () => (
  <ConfigScopeProvider scope={FEATURES_SCOPE}>
    <FeaturesPageBody />
  </ConfigScopeProvider>
);
