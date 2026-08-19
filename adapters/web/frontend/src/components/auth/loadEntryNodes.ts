// 登录成功后填入口节点列表。两个登录页共用：控制台不需要它，但登录状态是全局的，
// 从控制台登录后切回对话界面同样得有节点可选。
import { getNodes } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';

export async function loadEntryNodes(token: string): Promise<void> {
  const { setAvailableNodes, setEntryNodeId } = useSettingsStore.getState();
  try {
    const nodes = await getNodes(token);
    const aiNodes = nodes.filter((node: any) => node.type === 'ai' && !node.id.startsWith('system.'));
    setAvailableNodes(aiNodes);
    const saved = localStorage.getItem('clonoth_entry_node') || '';
    const savedIsValid = aiNodes.some((node: any) => node.id === saved);
    if ((!saved || !savedIsValid) && aiNodes.length > 0) setEntryNodeId(aiNodes[0].id);
  } catch {
    // 节点列表是可选装饰：拉不到不该把人挡在登录页外面。
  }
}

/** 令牌从哪来。只说「无效」等于让人去翻源码。 */
export const TOKEN_SOURCE_HINT = '首启日志里有带令牌的直达链接；也可以读 data/.admin_token，或用 CLONOTH_ADMIN_TOKEN 指定。';
