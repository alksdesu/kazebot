// 顶栏、会话面板、登录后加载、客户端设置和通用设置共用的几份读取：读到的数据和读没读成都记进 settings store。
// 请求守卫由调用方给，作废跟着调用方的规则走；store 那边读成过就不再记失败，几处谁先回来结果都一样。
import { errorMessage } from '../api/request';
import type { RequestTicket } from '../api/requestGuard';
import {
  activeProviderConfig,
  getActiveNode,
  getAppConfig,
  getNodes,
  getProviders,
  getSessionProviderOverride,
  type ActiveNode,
  type AdminNode,
  type AppConfigPublic,
  type ProvidersResponse,
  type SessionProviderOverride,
} from '../api/supervisorClient';
import { pickEntryNode, rootEntryNodes } from '../utils/entryNodes';
import { useSettingsStore } from './settingsStore';

/** 按会话读的那两份。没读成的原因由调用方自己留着，换会话就作废；这次已经作废时读取给 null。 */
export type SessionRead<T> = { readonly ok: true; readonly value: T } | { readonly ok: false; readonly error: string };

/**
 * 读回来的当前节点记进 store。writes 是发请求前记下的 activeNodeWrites：期间有人写过（面板刚切好的节点），
 * 读到的就是那之前的，不写，免得盖掉新的。写了给 true。
 */
export function adoptActiveNode(writes: number, nodeId: string, isOverride: boolean, defaultId: string, sid: string): boolean {
  const settings = useSettingsStore.getState();
  if (settings.activeNodeWrites !== writes) return false;
  settings.setActiveNode(nodeId, isOverride, defaultId, sid);
  return true;
}

/** 会话当前交给哪个节点，给节点 id。读的期间没人写过当前节点才记进 store。 */
export async function readActiveNode(sid: string, request: RequestTicket): Promise<SessionRead<string> | null> {
  const writes = useSettingsStore.getState().activeNodeWrites;
  let result: ActiveNode;
  try {
    result = await getActiveNode(sid);
  } catch (caught) {
    return request.isCurrent() ? { ok: false, error: errorMessage(caught) } : null;
  }
  if (!request.isCurrent()) return null;
  adoptActiveNode(writes, result.node_id, result.is_override, result.default_node_id, sid);
  return { ok: true, value: result.node_id };
}

/** 会话的模型覆盖。读到了记进 store，给读到的那份。 */
export async function readSessionOverride(sid: string, token: string, request: RequestTicket): Promise<SessionRead<SessionProviderOverride> | null> {
  let result: SessionProviderOverride;
  try {
    result = await getSessionProviderOverride(sid, token);
  } catch (caught) {
    return request.isCurrent() ? { ok: false, error: errorMessage(caught) } : null;
  }
  if (!request.isCurrent()) return null;
  useSettingsStore.getState().setSessionProviderOverride(result, sid);
  return { ok: true, value: result };
}

/** 全局在用渠道的模型和地址，以及后端配的默认入口。读成了、记进了 store 给 true。 */
export async function readGlobalConfig(request: RequestTicket): Promise<boolean> {
  let config: AppConfigPublic;
  try {
    config = await getAppConfig();
  } catch (caught) {
    if (request.isCurrent()) useSettingsStore.getState().failGlobalConfig(errorMessage(caught));
    return false;
  }
  if (!request.isCurrent()) return false;
  useSettingsStore.getState().setGlobalConfig(config.openai.model, config.openai.base_url, config.default_entry_node_id);
  return true;
}

/** 节点表。读到了给全部节点，没读成或者这次已经作废时给 null。 */
export async function readNodeList(token: string, request: RequestTicket): Promise<AdminNode[] | null> {
  let nodes: AdminNode[];
  try {
    nodes = await getNodes(token);
  } catch (caught) {
    if (request.isCurrent()) useSettingsStore.getState().failNodeList(errorMessage(caught));
    return null;
  }
  if (!request.isCurrent()) return null;
  useSettingsStore.getState().setNodeList(nodes);
  return nodes;
}

/** 渠道表。在用渠道的公开配置一并更新，插件读的 modelConfig 就是它。 */
export async function readProviderTable(token: string, request: RequestTicket): Promise<void> {
  let table: ProvidersResponse;
  try {
    table = await getProviders(token);
  } catch (caught) {
    if (request.isCurrent()) useSettingsStore.getState().failProviderTable(errorMessage(caught));
    return;
  }
  if (!request.isCurrent()) return;
  const settings = useSettingsStore.getState();
  settings.setProviderTable(table);
  settings.setModelConfig(activeProviderConfig(table));
}

/**
 * 读节点表和全局配置，替用户定入口：两样都读成过、存着的入口又不在根入口里，才换成 pickEntryNode 挑的那个。
 * 这次重读不成时沿用这次登录读成过的那份，和页面上摆的清单是同一份；全局配置一次都没读成就不挑：挑中的可能
 * 不是后端配的默认入口，而入口留空时新对话本来就走它。saved 在读完以后取存着的入口。
 */
export async function settleEntryNode(token: string, nodes: RequestTicket, config: RequestTicket, saved: () => string): Promise<void> {
  await Promise.all([readNodeList(token, nodes), readGlobalConfig(config)]);
  if (!nodes.isCurrent() || !config.isCurrent()) return;
  const settings = useSettingsStore.getState();
  if (!settings.nodeListRead.loaded || !settings.globalConfigRead.loaded) return;
  const roots = rootEntryNodes(settings.availableNodes);
  const current = saved();
  if (roots.some((node) => node.id === current)) return;
  const next = pickEntryNode(roots, settings.defaultNodeId);
  if (next !== current) settings.setEntryNodeId(next);
}
