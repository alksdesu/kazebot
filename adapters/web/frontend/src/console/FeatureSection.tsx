// 全局设置的一个区块：字段来自后端 schema，底部是这一块自己的保存条，只写回这一块摆出来的键。
import { useEffect, useMemo, useState } from 'react';

import type { ConsoleSectionSchema } from '../api/supervisorClient';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import { ErrorText, SaveBar } from './components';
import { ConfigScopeProvider, FEATURES_SCOPE } from './configScope';
import { useConsoleStore, useDraftCount, type CommitOutcome } from './consoleStore';
import { SchemaFields, type SchemaFieldMap } from './FieldRenderer';
import { fieldKeys } from './schemaNodes';
import { useConsoleSchema, useSchemaStore } from './schemaStore';
import { SectionAnchor } from './sectionOpen';
import type { SectionPick } from './sectionPick';

/** 表情包图库那一块在「表情包图库」页上保存，全局默认页不重复摆。 */
export const STICKER_LIBRARY_SECTION = 'sticker_library';

/** 全局默认页和表情包图库页各摆全局 schema 的哪几块。两页的认领不重不漏，侧栏搜索靠它把结果指到对的那页。 */
export const DEFAULTS_PAGE_SECTIONS: SectionPick = { except: [STICKER_LIBRARY_SECTION] };
export const STICKERS_PAGE_SECTIONS: SectionPick = { only: [STICKER_LIBRARY_SECTION] };

const SAVE_NOTE = '写进全局设置，没单独设置这一项的平台都跟着变。';

export const FeatureSectionBody = ({ fields, names, section }: {
  fields: SchemaFieldMap;
  /** 这一块摆出来的键。保存、丢弃、计数都只认它们。 */
  names: readonly string[];
  section: ConsoleSectionSchema;
}) => {
  const token = useSettingsStore((state) => state.adminToken);
  const pending = useDraftCount(names);
  const saving = useConsoleStore((state) => state.saving);
  const save = useConsoleStore((state) => state.save);
  const discard = useConsoleStore((state) => state.discard);
  const [outcome, setOutcome] = useState<CommitOutcome | null>(null);

  const commit = async () => {
    if (!token) return;
    setOutcome(null);
    setOutcome(await save(token, names));
  };

  // 保存成功的话只留到下一次改动；失败的原因一直挂着，直到再保存或丢弃。
  const shown = outcome && (!outcome.ok || pending === 0) ? outcome : null;

  return (
    <>
      <SchemaFields fields={fields} nodes={section.nodes} sectionId={section.id} />
      {shown && !shown.ok && <ErrorText>{shown.message}</ErrorText>}
      <SaveBar
        busy={saving}
        dirty={pending > 0}
        note={shown?.ok ? shown.message || '已保存。' : SAVE_NOTE}
        onReset={() => { setOutcome(null); discard(names); }}
        onSave={() => void commit()}
      />
    </>
  );
};

const StandaloneBody = ({ leaveMessage, sectionId }: { leaveMessage: string; sectionId: string }) => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const refresh = useConsoleStore((state) => state.refresh);
  const loadError = useConsoleStore((state) => state.error);
  const { schema, error: schemaError } = useConsoleSchema(FEATURES_SCOPE);
  const loadSchema = useSchemaStore((state) => state.load);
  const requested = useViewStore((state) => state.activeSettingsSection);
  const section = schema?.sections.find((part) => part.id === sectionId);
  const names = useMemo(() => (section ? fieldKeys(section.nodes) : []), [section]);
  const pending = useDraftCount(names);
  useUnsavedChanges(pending > 0, leaveMessage);

  useEffect(() => {
    if (!adminToken) return;
    void refresh(adminToken);
    void loadSchema(adminToken, FEATURES_SCOPE);
  }, [adminToken, loadSchema, refresh]);

  const problem = schemaError || loadError;
  if (problem) return <ErrorText>{problem}</ErrorText>;
  if (!schema || !section) return null;
  // 侧栏搜到这一块时带着分区 id 跳过来，页面上面还有别的块，得滚到它这儿。
  return (
    <SectionAnchor deepLinked={requested === sectionId} open>
      <FeatureSectionBody fields={schema.fields} names={names} section={section} />
    </SectionAnchor>
  );
};

/** 在别的页面里单独摆一块全局设置，比如表情包图库页的「入库」。自己拉全局那份和布局，自己拦未保存离开。 */
export const StandaloneFeatureSection = ({ leaveMessage, sectionId }: {
  /** 离开页面时的确认文案，说清是哪一块没保存。 */
  leaveMessage: string;
  sectionId: string;
}) => (
  <ConfigScopeProvider scope={FEATURES_SCOPE}>
    <StandaloneBody leaveMessage={leaveMessage} sectionId={sectionId} />
  </ConfigScopeProvider>
);
