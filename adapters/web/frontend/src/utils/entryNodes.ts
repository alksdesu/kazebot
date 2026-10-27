// 网页里能选作对话入口、能切过去的节点；不列模板、示例和 internal 节点，跟后端 switch_node 的枚举一致。
import type { AdminNode } from '../api/supervisorClient';

type EntryNodeFields = Partial<Pick<AdminNode, 'id' | 'type' | 'active' | 'internal' | 'problem'>>;

/** 模板、示例（active=false）运行期不当节点，internal 节点回的是给程序看的判定值，系统节点不给人选，节点文件用不了（problem）的 engine 加载不了。 */
export function isEntryNode(node: EntryNodeFields | null | undefined): boolean {
  return Boolean(
    node
    && node.type === 'ai'
    && typeof node.id === 'string'
    && !node.id.startsWith('system.')
    && node.active !== false
    && node.internal !== true
    && !node.problem,
  );
}

export function filterEntryNodes<T extends EntryNodeFields>(nodes: readonly T[]): T[] {
  return nodes.filter((node) => isEntryNode(node));
}
