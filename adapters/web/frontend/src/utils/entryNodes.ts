// 网页里能选作对话入口、能切过去的节点；不列模板、示例和 internal 节点，跟后端 switch_node 的枚举一致。
import type { AdminNode } from '../api/supervisorClient';

type EntryNodeFields = Partial<Pick<AdminNode, 'id' | 'type' | 'active' | 'internal' | 'problem' | 'delegate_targets' | 'selectable'>>;

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

/** 入口下拉和新对话默认入口列的节点，和 engine 给 switch_node 列的那份同一个算法（engine.node.entry_roots）：
 *  写了 selectable 的以它为准；没写的，被能当入口的节点委派出去才算子节点，委派给自己不算；一个都没有时列出没写 false 的全部。 */
export function rootEntryNodes<T extends EntryNodeFields & { id: string }>(nodes: readonly T[]): T[] {
  const entries = filterEntryNodes(nodes);
  const delegated = new Set<string>();
  for (const node of entries) {
    for (const target of node.delegate_targets || []) {
      if (target !== node.id) delegated.add(target);
    }
  }
  const offered = entries.filter((node) => node.selectable !== false);
  const roots = offered.filter((node) => node.selectable === true || !delegated.has(node.id));
  return roots.length > 0 ? roots : offered;
}

/** 替用户挑的入口：后端配的默认入口在根入口里就挑它，不在就挑第一个根入口；一个根入口都没有时是空串。 */
export function pickEntryNode(roots: readonly { readonly id: string }[], defaultId: string): string {
  return (roots.find((node) => node.id === defaultId) ?? roots[0])?.id ?? '';
}
