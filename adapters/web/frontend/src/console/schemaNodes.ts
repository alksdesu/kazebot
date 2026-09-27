// 控制台布局树的遍历：父节点先于子节点，和后端 console_schema.walk 同序。
import type { ConsoleNode } from '../api/supervisorClient';

export function* walkNodes(nodes: readonly ConsoleNode[] | undefined): Generator<ConsoleNode> {
  for (const node of nodes || []) {
    yield node;
    if ('children' in node) yield* walkNodes(node.children);
  }
}

/** 一段布局里摆了哪些键，按出现顺序。区块自己保存时只写回这些。 */
export function fieldKeys(nodes: readonly ConsoleNode[]): string[] {
  return [...walkNodes(nodes)].flatMap((node) => (node.kind === 'field' ? [node.key] : []));
}
