// 登录成功后填入口节点列表。两个登录页共用：控制台不需要它，但登录状态是全局的，
// 从控制台登录后切回对话界面同样得有节点可选。
import { RequestScope } from '../../api/requestGuard';
import { LS_KEY_NODE, useSettingsStore } from '../../store/settingsStore';
import { settleEntryNode } from '../../store/settingsReads';

// 登录页早就卸掉了，节点列表还在路上：作废只看登录换没换，不看页面在不在。
const entryNodes = new RequestScope();

function savedEntryNode(): string {
  let saved = useSettingsStore.getState().entryNodeId || '';
  try { saved = localStorage.getItem(LS_KEY_NODE) || saved; } catch {}
  return saved;
}

export async function loadEntryNodes(token: string): Promise<void> {
  // 节点表和全局配置是可选装饰：拉不到不该把人挡在登录页外面。没读成的原因记在 store 里，用到它们的地方会说出来。
  // 存着的入口不在根入口里时才换：模板和示例收到消息直接失败，internal 节点只回给程序看的判定值，
  // 别的入口委派下去的子节点（命令审核这种）不该当默认入口。
  await settleEntryNode(token, entryNodes.begin('nodes'), entryNodes.begin('config'), savedEntryNode);
}

/** 令牌从哪来。只说「无效」等于让人去翻源码。 */
export const TOKEN_SOURCE_HINT = '首启日志里有带令牌的直达链接；也可以读 data/.admin_token，或用 CLONOTH_ADMIN_TOKEN 指定。';
