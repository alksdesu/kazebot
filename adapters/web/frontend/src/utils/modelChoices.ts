// Model id candidates for the console node editor, the provider editor, and session overrides.
// The upstream relay defines the value space, so these lists are hints for a combobox, never a closed set.
import type { AdminNode, ProvidersResponse } from '../api/supervisorClient';

const asModel = (value: unknown): string => (typeof value === 'string' ? value.trim() : '');

// Unsorted on purpose: active channel, then the other channels, then the fallback chain, so reading order carries priority.
export function modelsFromProviders(data: ProvidersResponse | null | undefined, exclude = ''): string[] {
  const providers = data?.providers ?? {};
  const active = data?.active_provider ?? '';
  const fallbacks = Array.isArray(data?.fallbacks) ? data.fallbacks : [];
  const own = exclude ? asModel(providers[exclude]?.model) : '';
  return [
    asModel(providers[active]?.model),
    ...Object.entries(providers)
      .filter(([name]) => name !== active)
      .map(([, config]) => asModel(config?.model)),
    ...fallbacks.map((entry) => asModel(entry?.model)),
    // 按值而不是按渠道名排除：fallbacks 里重复写一遍本渠道的 model 时，按名剔除挡不住它。
  ].filter((model) => !own || model !== own);
}

// 环境变量模板留给会走 resolve_env_ref 的那些字段。会话覆盖不展开它（engine/model.py 的
// resolve_provider 直接赋值），摆进候选等于递给用户一个必挂的值。
// 比 envRef.ts 那条松：这里只需判断"能不能直接用"，漏挡一个必挂的值比多挡一个候选糟。
const ENV_TEMPLATE = /\$(?:ENV)?\{/;

export function literalModelsOnly(models: string[]): string[] {
  return models.filter((model) => !ENV_TEMPLATE.test(model));
}

export function modelsFromNodes(nodes: AdminNode[] | null | undefined, exclude: string): string[] {
  // 和 modelsFromProviders 一样先验形状：候选拉不到是常态，不该把整个面板拖崩。
  if (!Array.isArray(nodes)) return [];
  return nodes.filter((node) => node.id !== exclude).map((node) => asModel(node.model));
}

export function mergeModelChoices(...lists: string[][]): string[] {
  return [...new Set(lists.flat())].filter(Boolean);
}
