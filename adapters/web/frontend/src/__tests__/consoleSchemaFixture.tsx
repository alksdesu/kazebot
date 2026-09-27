// 后端控制台 schema 的快照。由 tests/_console_fixture.py 生成，test_console_schema 盯着它和后端声明逐项一致，
// 页面测试拿它画分区，测的就是后端真正会给的那份布局。
import type { ConsoleSchema } from '../api/supervisorClient';
import { FEATURES_SCOPE, platformScope, type ConfigScope } from '../console/configScope';
import { SchemaFields } from '../console/FieldRenderer';
import { QQ_PARTS } from '../console/qqParts';
import { useSchemaStore } from '../console/schemaStore';
import { PartsProvider } from '../console/sectionParts';
import snapshot from './fixtures/consoleSchemas.json';

// JSON 推出来的是宽类型（kind 是 string、choices 是 string[][]），按接口类型收窄。
export const QQ_SCHEMA = snapshot.platforms.qq as unknown as ConsoleSchema;
export const FEATURES_SCHEMA = snapshot.features as unknown as ConsoleSchema;

/** 某个作用域在快照里的那份 schema。快照里没有这个平台时是 undefined。 */
export function snapshotFor(scope: ConfigScope): ConsoleSchema | undefined {
  if (scope.kind === 'features') return FEATURES_SCHEMA;
  return (snapshot.platforms as Record<string, unknown>)[scope.platformId] as ConsoleSchema | undefined;
}

/** 把快照灌进 schema 缓存：页面不发请求就能画出分区。 */
export function seedConsoleSchemas(): void {
  const platforms = Object.keys(snapshot.platforms).map((id) => [platformScope(id).id, snapshotFor(platformScope(id))]);
  useSchemaStore.setState({
    schemas: { ...Object.fromEntries(platforms), [FEATURES_SCOPE.id]: FEATURES_SCHEMA },
    errors: {},
  });
}

export function clearConsoleSchemas(): void {
  useSchemaStore.setState({ schemas: {}, errors: {} });
}

/** 按快照画 QQ 的一个分区正文，带上 QQ 页自己的那几块，和平台页里展开这个分区看到的一样。 */
export const QqSectionBody = ({ id }: { id: string }) => {
  const section = QQ_SCHEMA.sections.find((part) => part.id === id);
  if (!section) throw new Error(`快照里没有 QQ 分区 ${id}`);
  return (
    <PartsProvider parts={QQ_PARTS}>
      <SchemaFields fields={QQ_SCHEMA.fields} nodes={section.nodes} sectionId={id} />
    </PartsProvider>
  );
};
