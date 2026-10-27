// 登录成功后填入口节点列表。两个登录页共用：控制台不需要它，但登录状态是全局的，
// 从控制台登录后切回对话界面同样得有节点可选。
import { RequestScope } from '../../api/requestGuard';
import { getNodes } from '../../api/supervisorClient';
import { LS_KEY_NODE, useSettingsStore } from '../../store/settingsStore';
import { filterEntryNodes } from '../../utils/entryNodes';

// 登录页早就卸掉了，节点列表还在路上：作废只看登录换没换，不看页面在不在。
const entryNodes = new RequestScope();

export async function loadEntryNodes(token: string): Promise<void> {
  const request = entryNodes.begin('nodes');
  const { setAvailableNodes, setEntryNodeId } = useSettingsStore.getState();
  try {
    const nodes = await getNodes(token);
    if (!request.isCurrent() || !Array.isArray(nodes)) return;
    // 存着的入口是模板、示例或 internal 节点时换成第一个能选的：模板和示例收到消息直接失败，internal 节点只回给程序看的判定值。
    const selectable = filterEntryNodes(nodes);
    setAvailableNodes(selectable);
    let saved = useSettingsStore.getState().entryNodeId || '';
    try { saved = localStorage.getItem(LS_KEY_NODE) || saved; } catch {}
    const savedIsValid = selectable.some((node) => node.id === saved);
    if (!savedIsValid) setEntryNodeId(selectable[0]?.id || '');
  } catch {
    // 节点列表是可选装饰：拉不到不该把人挡在登录页外面。
  }
}

/** 令牌从哪来。只说「无效」等于让人去翻源码。 */
export const TOKEN_SOURCE_HINT = '首启日志里有带令牌的直达链接；也可以读 data/.admin_token，或用 CLONOTH_ADMIN_TOKEN 指定。';
