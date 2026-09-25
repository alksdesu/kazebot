import type { MemoryOwner } from '../api/supervisorClient';

export interface ConversationDescriptor {
  scope: string;
  bot_scope?: string | null;
  owner?: MemoryOwner | null;
  current_account?: boolean;
}

export const scopeIdentity = (scope: string, botScope: string | null = null) => JSON.stringify([scope, botScope]);

function cleanName(value: unknown): string {
  return typeof value === 'string' ? value.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, '').trim().slice(0, 96) : '';
}

function shortIdentity(value: string): string {
  let hash = 2166136261;
  for (const character of value) hash = Math.imul(hash ^ character.codePointAt(0)!, 16777619);
  return (hash >>> 0).toString(16).padStart(8, '0');
}

function kindName(row: ConversationDescriptor): string {
  if (row.owner?.kind === 'agent') return '子代理会话';
  if (row.scope.startsWith('qq_group:') || row.owner?.kind === 'group') return '群聊';
  if (row.scope.startsWith('qq_private:') || row.owner?.kind === 'private') return '私聊';
  return row.scope.startsWith('web:') ? '网页会话' : '会话';
}

function baseName(row: ConversationDescriptor, webTitles: ReadonlyMap<string, string>): string {
  if (row.scope === 'web:console') return '控制台会话（非全局）';
  if (row.scope === 'web:materials') return '资料工作区';
  const kind = kindName(row);
  const label = cleanName(row.owner?.label);
  const alias = cleanName(row.owner?.alias);
  const realId = cleanName(row.owner?.real_id);
  if (row.owner?.name_available === true && label) return label;
  const technical = (name: string) => name === row.scope || /^(?:qq_group:|qq_private:|agent:|conv[-_]|session[_-]|[a-f\d]{20,}$)/i.test(name);
  if (row.owner?.name_available === false && alias && !technical(alias)) return `${kind}（匿名别名：${alias}）`;
  if (row.owner?.name_available !== false && label && !technical(label)) {
    if (alias && label === alias) return `${kind}（匿名别名：${alias}）`;
    if (!/^(?:群|群聊|私聊|未知来源)(?:（名称暂不可用）)?$/.test(label) && !(realId && [`群 ${realId}`, `私聊 ${realId}`].includes(label))) return label;
  }
  const title = row.scope.startsWith('web:') && row.current_account !== false && (row.bot_scope == null || row.current_account === true) ? cleanName(webTitles.get(row.scope)) : '';
  if (title && !technical(title)) return title;
  return `${kind}（名称暂不可用） · ${shortIdentity(scopeIdentity(row.scope, row.bot_scope))}`;
}

export function conversationNames(rows: readonly ConversationDescriptor[], webTitles: ReadonlyMap<string, string> = new Map()): Map<string, string> {
  const unique = new Map(rows.map(row => [scopeIdentity(row.scope, row.bot_scope), row]));
  const base = new Map([...unique].map(([key, row]) => [key, baseName(row, webTitles)]));
  const counts = new Map<string, number>();
  for (const label of base.values()) counts.set(label, (counts.get(label) || 0) + 1);
  const labels = new Map<string, string>();
  const used = new Set<string>();
  for (const [key, row] of [...unique].sort(([a], [b]) => a.localeCompare(b))) {
    const name = base.get(key)!;
    let label = counts.get(name)! > 1 ? `${name} · ${kindName(row)} ${shortIdentity(key)}` : name;
    if (row.current_account === false) label += '（历史账号）';
    const original = label;
    for (let index = 2; used.has(label); index++) label = `${original} · ${index}`;
    used.add(label); labels.set(key, label);
  }
  return labels;
}
