// 侧栏搜索的分区索引：分区名之外，把块标题、并排格名和字段标签也收进来，折叠着的「冷却」照样搜得到。
import type { ConsoleFieldSchema, ConsoleNode, ConsoleSchema } from '../api/supervisorClient';

export interface SearchableSection {
  id: string;
  label: string;
  /** 小写后的全部可搜文字。 */
  text: string;
}

type FieldMap = Readonly<Record<string, ConsoleFieldSchema>>;

function labelsIn(nodes: readonly ConsoleNode[] | undefined, fields: FieldMap): string[] {
  return (nodes || []).flatMap((node) => {
    switch (node.kind) {
      case 'block':
        return [node.title, ...labelsIn(node.children, fields)];
      case 'fixed':
        return [node.label, ...labelsIn(node.children, fields)];
      case 'field':
        return [fields[node.key]?.label || '', ...labelsIn(node.children, fields)];
      case 'grid':
      case 'panel':
      case 'loose':
        return labelsIn(node.children, fields);
      default:
        return [];
    }
  });
}

// 每敲一个字都要重扫；schema 拉下来之后不会变，按对象缓存。
const cache = new WeakMap<ConsoleSchema, readonly SearchableSection[]>();

export function searchableSections(schema: ConsoleSchema): readonly SearchableSection[] {
  const cached = cache.get(schema);
  if (cached) return cached;
  const sections = schema.sections.map((section) => ({
    id: section.id,
    label: section.label,
    text: [section.label, section.id, ...labelsIn(section.nodes, schema.fields)].join(' ').toLocaleLowerCase(),
  }));
  cache.set(schema, sections);
  return sections;
}
