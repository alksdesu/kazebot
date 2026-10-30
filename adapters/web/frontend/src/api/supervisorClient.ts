// Supervisor 接口的薄封装：只管路径和请求体，鉴权、时限与报错交给 request。
import type { NodeDef, SupervisorEvent } from '../types';
import { nameWith } from '../console/format';
import { commandAction } from '../plugins/commands';
import { isInstancePath, MOUNT } from '../instances/paths';
import { ApiError, currentAdminToken, request, send, type RequestOptions } from './request';
import {
  BOOLEAN, NON_EMPTY_TEXT, NUMBER, OBJECT, PRESENT, TEXT, TEXT_LIST,
  ReceiptUnreadable, at, formatError, isJsonObject, jsonOf, listOf, nullable, oneOf, optional, receiptOf, recordOf,
  shaped, shapedRecord, shapedRows, shapedValue,
  type Kind, type Shape, type Shaped,
} from './shape';

export { MOUNT, instanceConsoleHref } from '../instances/paths';
export { ApiError, isEditConflict, isNotFound, isTransientApiError } from './request';
export { ReceiptUnreadable, receipted, type Receipted } from './shape';

const API = `${MOUNT}/v1`;

function apiFetch(path: string, options?: RequestOptions): Promise<Response> {
  return request(`${API}${path}`, options);
}

/** 附件在本实例上的地址。path 是工作区相对路径，例如 data/attachments/xxx/yyy.png；令牌走请求头，不进地址。 */
export function attachmentUrl(path: string): string {
  return `${API}/files/${path.split('/').map(encodeURIComponent).join('/')}`;
}

// ── Attachment upload ──

export interface UploadedAttachment {
  path: string;
  name: string;
  size: number;
  mime_type: string;
  type: 'image' | 'file';
}

const UPLOADED_ATTACHMENT = {
  path: NON_EMPTY_TEXT, name: TEXT, size: NUMBER, mime_type: TEXT, type: oneOf('image', 'file'),
};

/** 传上了却读不懂回执时抛 ReceiptUnreadable：文件已经在服务器上，只是不知道它的路径，引用不了。 */
export async function uploadAttachment(
  file: File,
  conversationKey: string,
  token?: string,
): Promise<UploadedAttachment> {
  const form = new FormData();
  form.append('file', file);
  // 不手动设 Content-Type：浏览器要自己填 multipart boundary。
  const resp = await apiFetch(`/attachments/upload?conversation_key=${encodeURIComponent(conversationKey)}`, {
    method: 'POST', body: form, token,
  });
  const receipt = nameWith(`附件 ${file.name}`, '已经传上，可回执');
  return receiptOf(receipt, resp, (body) => shaped(receipt, body, UPLOADED_ATTACHMENT));
}

// ── Inbound (send message) ──

export interface InboundReceipt {
  session_id: string;
  inbound_seq: number;
  accepted: boolean;
}

const INBOUND_RECEIPT = { session_id: NON_EMPTY_TEXT, inbound_seq: NUMBER, accepted: BOOLEAN };

/** 发出了却读不懂回执时抛 ReceiptUnreadable：消息已经进了服务器，会话号要等事件流里的回显补上。 */
export async function postInbound(params: {
  conversation_key: string;
  text: string;
  attachments?: any[];
  use_context?: boolean;
  entry_node_id?: string;
}): Promise<InboundReceipt> {
  const resp = await apiFetch('/inbound', {
    method: 'POST',
    json: {
      channel: 'web',
      conversation_key: params.conversation_key,
      text: params.text,
      attachments: params.attachments ?? [],
      use_context: params.use_context ?? true,
      entry_node_id: params.entry_node_id,
    },
  });
  const receipt = '消息已经发出，可回执';
  return receiptOf(receipt, resp, (body) => shaped(receipt, body, INBOUND_RECEIPT));
}

// ── 全局事件流 ──

export interface GlobalStreamHandlers {
  onEvent: (event: SupervisorEvent) => void;
  /** 后端认过令牌、开始推送了。reconnected：断开过又连上了。后端只推连上之后的新事件，断开期间的要调用方自己重拉。 */
  onOpen?: (info: { reconnected: boolean }) => void;
  /** retryInMs：多久之后自动重连。平常按断开次数指数退避，后端说了要等多久就按它说的。 */
  onClose?: (info: { retryInMs: number }) => void;
  /** 后端不认这个令牌（没带或不对）：不再自动重连，得让人重新登录。 */
  onUnauthorized?: () => void;
}

// 数值而不是 WebSocket.CONNECTING：测试和老浏览器替换的 WebSocket 上不一定挂着这些静态常量。
const SOCKET_CONNECTING = 0;
const SOCKET_OPEN = 1;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30_000;
// 后端的关闭码：4401 令牌被拒；4429 认证失败太多、正在退避，reason 里写着要等几秒。
const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_THROTTLED = 4429;

let globalSocket: WebSocket | null = null;
let globalHandlers: GlobalStreamHandlers | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectDelay = RECONNECT_BASE_MS;
let openedBefore = false;

function clearReconnectTimer(): void {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
}

function nextBackoffMs(): number {
  const delay = reconnectDelay;
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
  return delay;
}

/** 4429 时后端要求等的毫秒数；reason 里读不出秒数给 null，按平常的退避算。 */
function throttledRetryMs(reason: string): number | null {
  const seconds = Number(/\d+(?:\.\d+)?/.exec(reason)?.[0]);
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds * 1000) : null;
}

function openGlobalSocket(): void {
  clearReconnectTimer();
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${wsProtocol}//${window.location.host}${API}/ws`);
  globalSocket = socket;
  let ready = false;

  // 被拒的连接也是先 open 再关：退避要等后端认过令牌才清零，不然每次都从一秒重来。
  const markReady = () => {
    if (ready) return;
    ready = true;
    reconnectDelay = RECONNECT_BASE_MS;
    const reconnected = openedBefore;
    openedBefore = true;
    globalHandlers?.onOpen?.({ reconnected });
  };

  socket.onopen = () => {
    if (globalSocket !== socket) return;
    // 后端拿首帧里的令牌认证，认过才开始推送；last_seq 不补发历史，补齐靠调用方重拉。
    socket.send(JSON.stringify({ last_seq: 0, token: currentAdminToken() }));
  };

  socket.onmessage = (message) => {
    let data: unknown;
    try {
      data = JSON.parse(message.data);
    } catch {
      return;
    }
    // 认不出的帧不报：事件流没有回执，报了也没人能处理，下一次同步会把漏掉的补上。
    if (!isJsonObject(data) || typeof data.type !== 'string') return;
    // stream_ready 是认证通过的信号；先到的心跳或事件同样说明认证过了。
    markReady();
    if (data.type === 'stream_ready' || data.type === 'ping') return;
    globalHandlers?.onEvent(data as unknown as SupervisorEvent);
  };

  socket.onclose = (event) => {
    if (globalSocket !== socket) return;
    globalSocket = null;
    if (event.code === CLOSE_UNAUTHORIZED) {
      globalHandlers?.onUnauthorized?.();
      return;
    }
    const retryInMs = (event.code === CLOSE_THROTTLED ? throttledRetryMs(event.reason) : null) ?? nextBackoffMs();
    reconnectTimer = setTimeout(openGlobalSocket, retryInMs);
    globalHandlers?.onClose?.({ retryInMs });
  };

  socket.onerror = () => {
    socket.close();
  };
}

/** 订阅全部会话的事件。已连着就只换回调；正在等重连时立刻重试一次。断开后自动重连，令牌被拒除外。 */
export function connectGlobalWS(handlers: GlobalStreamHandlers): void {
  globalHandlers = handlers;
  const readyState = globalSocket?.readyState;
  if (readyState === SOCKET_CONNECTING || readyState === SOCKET_OPEN) return;
  openGlobalSocket();
}

/** 彻底断开，不再重连。退出登录、重置状态时用。 */
export function disconnectGlobalWS(): void {
  clearReconnectTimer();
  globalHandlers = null;
  openedBefore = false;
  reconnectDelay = RECONNECT_BASE_MS;
  const socket = globalSocket;
  globalSocket = null;
  if (!socket) return;
  socket.onopen = null;
  socket.onmessage = null;
  socket.onclose = null;
  socket.onerror = null;
  socket.close();
}

// ── Health ──

export interface HealthState {
  status: string;
  run_id?: string;
  workspace_root?: string;
  started_at?: string;
  uptime_seconds?: number;
}

/** 连接状态灯的健康检查：十秒没应答就算断开，不等读请求默认的那一分钟。 */
export const HEALTH_TIMEOUT_MS = 10_000;

// 匿名请求只回 status 和运行时长，run_id、workspace_root、started_at 只给带令牌的。
const HEALTH = {
  status: TEXT,
  run_id: optional(TEXT),
  workspace_root: optional(TEXT),
  started_at: optional(TEXT),
  uptime_seconds: optional(NUMBER),
};

export async function checkHealth(signal?: AbortSignal, timeoutMs?: number): Promise<HealthState> {
  const what = '健康检查';
  const resp = await apiFetch('/health', { signal, anonymous: true, timeoutMs });
  return shaped(what, await jsonOf(what, resp), HEALTH);
}

export interface AdminApproval {
  approval_id: string;
  session_id?: string;
  operation: string;
  details?: Record<string, unknown>;
  status?: string;
  fingerprint?: string;
  requested_at?: string;
  decided_at?: string | null;
  decision?: 'allow' | 'deny' | null;
  comment?: string | null;
  tool_call_id?: string | null;
  node_id?: string | null;
  task_id?: string | null;
  /** 没人定案、随任务失效被系统收掉时给人看的原因。 */
  expired_reason?: string | null;
}

const ADMIN_APPROVAL = {
  approval_id: NON_EMPTY_TEXT,
  session_id: optional(TEXT),
  operation: TEXT,
  details: optional(OBJECT),
  status: optional(TEXT),
  fingerprint: optional(TEXT),
  requested_at: optional(TEXT),
  decided_at: optional(nullable(TEXT)),
  decision: optional(nullable(oneOf('allow', 'deny'))),
  comment: optional(nullable(TEXT)),
  tool_call_id: optional(nullable(TEXT)),
  node_id: optional(nullable(TEXT)),
  task_id: optional(nullable(TEXT)),
  expired_reason: optional(nullable(TEXT)),
};

export interface AdminState {
  sessions: number;
  approvals: Record<string, number>;
  tasks: Record<string, number>;
  pending_approvals: AdminApproval[];
  engine_runtime: Record<string, unknown>;
}

// 计数补成 0、待审批补成空表，页面就会说「没有任务」「没有待审批」。
const ADMIN_STATE = {
  sessions: NUMBER,
  approvals: recordOf(NUMBER),
  tasks: recordOf(NUMBER),
  engine_runtime: OBJECT,
};

export interface ActiveTask {
  task_id: string;
  session_id: string;
  node_id: string | null;
  status: 'running' | 'pending' | 'suspended';
  kind: 'node' | 'tool';
  created_at: string;
  updated_at: string;
  worker_id: string | null;
  caller_task_id: string | null;
  // [AutoC 2026-06-04] Why: the active-task modal should identify work without
  // downloading the full task input. How: mirror the backend's capped preview and
  // explicit cancellation flag. Purpose: UI rows can show context and disable
  // duplicate cancellation requests from typed data.
  input_summary: string;
  cancel_requested: boolean;
  current_phase: string;
  current_detail: string;
  /** 排在哪一道，见 TaskLane。只拿来分组：分道之前的后端不给，认不出的道名归进「未分道」。 */
  lane?: string;
  /** 在等人工审批：这段时间不占 engine 的并发名额。 */
  awaiting_approval: boolean;
}

const ACTIVE_TASK = {
  task_id: NON_EMPTY_TEXT,
  session_id: TEXT,
  node_id: nullable(TEXT),
  status: oneOf('running', 'pending', 'suspended'),
  kind: oneOf('node', 'tool'),
  created_at: TEXT,
  updated_at: TEXT,
  worker_id: nullable(TEXT),
  caller_task_id: nullable(TEXT),
  input_summary: TEXT,
  cancel_requested: BOOLEAN,
  current_phase: TEXT,
  current_detail: TEXT,
  lane: optional(TEXT),
  awaiting_approval: BOOLEAN,
};

/** 领任务时排哪一道：快道是接话判断，交互道是用户在等的，后台道是压缩、记忆提取这类系统流程。 */
export type TaskLane = 'fast' | 'interactive' | 'background';

/**
 * 配置文件从哪来。仓库默认直接生效，改过就在 config/ 下存一份本实例的，没改过的跟着代码更新。
 * default：仓库默认原样；override：改过的仓库默认；custom：本实例自己加的；system：跟着代码走的系统节点，改不了。
 */
export type OverlaySource = 'default' | 'override' | 'custom' | 'system';

export interface AdminNode extends NodeDef {
  tool_access?: unknown;
  skills?: unknown;
  /** false = 模板或示例文件，不会被派发到。 */
  active?: boolean;
  /** true = 回给程序看的判定值，不接用户对话；入口和切换列表不列它。旧后端没有这一项。 */
  internal?: boolean;
  /** 节点 YAML 写了 selectable 就以它为准列不列进入口下拉，null = 没写，照委派关系推断。旧后端没有这一项。 */
  selectable?: boolean | null;
  /** yaml 里写的 id，且和文件名不一致。engine 认文件名，这个字段改了不换节点。 */
  declared_id?: string;
  source?: OverlaySource;
  /** 改过却和仓库默认一字不差：什么也没改，只会挡住以后默认的更新。 */
  same_as_default?: boolean;
  /** 非空 = 节点文件读不出来，或读出来不是能用的节点定义，engine 加载不了它；只有位置和问题，不带原文。 */
  problem?: string;
}

export type ToolSource = 'builtin' | 'plugin' | 'external';

/** 外部脚本从哪来：本实例自己加的、改过仓库那份的、仓库原样的。改仓库那份会在 config/tools/ 存一份本实例的。 */
export type ToolOrigin = 'custom' | 'override' | 'bundled';

export interface AdminTool {
  name: string;
  source: ToolSource;
  /** 只有外部脚本工具有源码文件，内置和插件工具打不开编辑器。 */
  editable: boolean;
  file?: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  timeout_sec?: number;
  has_spec?: boolean;
  origin?: ToolOrigin;
}

/** 删掉本实例那份配置的结果。restored = 删的是改过的仓库默认，现在回到了默认。 */
export interface ConfigRemoveResult {
  ok: boolean;
  restored: boolean;
}

export interface AdminSkill {
  /** frontmatter 里写的名字，没写是目录名。 */
  name: string;
  /** 技能目录名，读、写、删都按它找文件。 */
  dir?: string;
  path?: string;
  description?: string;
  enabled?: boolean;
  strategy?: string;
  keywords?: string[];
  body_preview?: string;
  /** 非空 = 文件读不了或 frontmatter 写坏了，加载时整个技能被跳过；只有位置和问题，不带原文。 */
  problem?: string;
  /** output 策略的 trigger / scope / max_fires 写得不对，这条规则不生效。 */
  rule_error?: string;
}

/** 技能文件在哪个目录：frontmatter 里的 name 可以和目录名不一样，拿 name 去读写会找错文件。 */
export function skillDir(skill: AdminSkill): string {
  return skill.dir || skill.name;
}

/** mcp.yaml 里的一项，后端已照运行时的读法规整：开关是布尔，传输方式认不出的按 stdio，命令、参数、地址都是字符串。 */
export interface McpClient {
  id: string;
  description?: string;
  enabled: boolean;
  transport?: string;
  command?: string;
  url?: string;
  [key: string]: unknown;
}

export interface AdminCreatePayload {
  id: string;
  content: string;
}

/**
 * 原文连同读到它时的指纹。编辑器自己留着 hash，保存时原样交回：文件中途被别处改过
 * （共享组里别的实例、另一个标签页、同一页上的另一个面板），后端回 409，一个字节都不写。
 * 空串表示读的时候文件还不存在。读回来的总有指纹；null 只出现在手上没有读成过的那一版上，保存时不带基准整份覆盖。
 */
export interface RawText {
  content: string;
  hash: string | null;
}

/** 保存之后磁盘上那一版的指纹，拿它当下一次保存的基准。 */
export interface RawSaveResult {
  ok: boolean;
  hash: string;
  /** 后端保存前校验给的提醒：能存，但有该知道的事（比如某个键已经没人读了）。没有提醒时不带这一项。 */
  warnings?: string[];
}

const SAVE_RECEIPT = { ok: BOOLEAN, hash: TEXT, warnings: optional(TEXT_LIST) };

/**
 * 写配置文件的回执：原文编辑器保存和新建文件回的是同一种。receipt 打头说清已经存上。
 * 指纹读不懂抛 ReceiptUnreadable：拿 null 当下一次保存的基准，保存时不带指纹，别处的改动会被整份盖掉。
 */
function saveResultOf(receipt: string, resp: Response): Promise<RawSaveResult> {
  return receiptOf(receipt, resp, (body) => {
    const { ok, hash, warnings } = shaped(receipt, body, SAVE_RECEIPT);
    return { ok, hash, ...(warnings?.length ? { warnings } : {}) };
  });
}

const savedReceipt = (what: string): string => nameWith(what, '已经存上，可回执');
const createdReceipt = (what: string): string => nameWith(what, '已经建好，可回执');
const removedReceipt = (what: string): string => nameWith(what, '本实例那份已经删掉，可回执');

const CONFIG_REMOVE = { ok: BOOLEAN, restored: BOOLEAN };

// 删了却读不懂回执时抛 ReceiptUnreadable：当成没删，人会再点一次，第二次只会撞上「没改过、删不掉」。
async function removeResultOf(receipt: string, resp: Response): Promise<ConfigRemoveResult> {
  const { ok, restored } = await receiptOf(receipt, resp, (body) => shaped(receipt, body, CONFIG_REMOVE));
  if (!ok) throw new Error('服务端没有删掉这份配置，请重试');
  return { ok, restored };
}

function rawWriteFields(content: string, baseHash: string | null): { content: string; base_hash?: string } {
  return baseHash === null ? { content } : { content, base_hash: baseHash };
}

const RAW_TEXT = { content: TEXT, hash: TEXT };

// 正文或指纹对不上形状就报格式不对：拿空串顶上，编辑器摆出来的是一份空文件；拿 null 顶上，保存时不带指纹，把真文件整份盖掉。
async function readRawConfig(what: string, path: string, token: string): Promise<RawText> {
  const resp = await apiFetch(path, { token });
  const { content, hash } = shaped(what, await jsonOf(what, resp), RAW_TEXT);
  return { content, hash };
}

async function writeRawConfig(
  what: string, path: string, token: string, content: string, baseHash: string | null,
): Promise<RawSaveResult> {
  const resp = await apiFetch(path, {
    method: 'PUT',
    token,
    json: rawWriteFields(content, baseHash),
  });
  return saveResultOf(savedReceipt(what), resp);
}

const pathPart = (value: string): string => encodeURIComponent(value);

export async function getAdminState(token: string): Promise<AdminState> {
  const what = '管理状态';
  const resp = await apiFetch('/admin/state', { token });
  const body = shaped(what, await jsonOf(what, resp), ADMIN_STATE);
  return { ...body, pending_approvals: shapedRows(what, body.pending_approvals, ADMIN_APPROVAL, 'pending_approvals') as AdminApproval[] };
}

// 补成空表就是「没有在跑的任务」：任务监视里的取消按钮跟着消失。
export async function fetchActiveTasks(token?: string): Promise<ActiveTask[]> {
  const what = '活跃任务列表';
  const resp = await apiFetch('/admin/tasks/active', { token });
  return shapedRows(what, await jsonOf(what, resp), ACTIVE_TASK);
}

/** 取消单个任务。cancelActiveTasks 管的是整个会话。 */
export async function cancelTask(token: string, taskId: string): Promise<void> {
  await apiFetch(`/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST', token });
}

// ── Admin auth ──

/** 令牌被拒返回 false；服务端故障抛错，让调用方分清「令牌不对」和「验证不了」。 */
export async function verifyAdminAuth(token: string, signal?: AbortSignal): Promise<boolean> {
  const resp = await send(`${API}/admin/auth/check`, { token, signal });
  if (resp.status === 401 || resp.status === 403) return false;
  if (!resp.ok) throw new ApiError(resp.status, '登录验证服务暂不可用');
  return true;
}

export async function checkAdminAuth(token: string): Promise<boolean> {
  try {
    return await verifyAdminAuth(token);
  } catch {
    return false;
  }
}

// ── Nodes ──

const NODE_ROW = {
  id: NON_EMPTY_TEXT,
  type: TEXT,
  name: optional(TEXT),
  description: optional(TEXT),
  model: optional(TEXT),
  base_url: optional(TEXT),
  provider: optional(TEXT),
  delegate_targets: optional(TEXT_LIST),
  aliases: optional(TEXT_LIST),
  active: optional(BOOLEAN),
  internal: optional(BOOLEAN),
  selectable: optional(nullable(BOOLEAN)),
  declared_id: optional(TEXT),
  source: optional(oneOf('default', 'override', 'custom', 'system')),
  same_as_default: optional(BOOLEAN),
  problem: optional(TEXT),
};

export async function getNodes(token: string): Promise<AdminNode[]> {
  const what = '节点表';
  const resp = await apiFetch('/admin/config/nodes', { token });
  return shapedRows(what, await jsonOf(what, resp), NODE_ROW) as unknown as AdminNode[];
}

export function getConfigRaw(token: string): Promise<RawText> {
  return readRawConfig('models.yaml 原文', '/admin/config/config/raw', token);
}

export function updateConfigRaw(token: string, yaml: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig('models.yaml', '/admin/config/config/raw', token, yaml, baseHash);
}

// ── Multi-provider config ──

export interface ProviderConfigPublic {
  base_url: string;
  model: string;
  // 展开前的原文。编辑器回填这一份，否则保存一次就把 ${VAR} 烧成了当时的展开值。
  base_url_raw: string;
  model_raw: string;
  api_key_present: boolean;
  api_key_redacted: string;
  /** 收不收图片。null = 没配，跟随这家 provider 的默认。 */
  supports_vision: boolean | null;
  /** 线格式。渠道名随便起，这个才决定请求按谁的格式发。 */
  type: string;
  /** 块里真写了 type，false 表示是从渠道名猜的。 */
  type_explicit: boolean;
  label: string;
}

// 备选条目靠 provider 指向某个渠道，自己不带 type/label。
export interface FallbackEntryPublic
  extends Omit<ProviderConfigPublic, 'supports_vision' | 'type' | 'type_explicit' | 'label'> {
  provider: string;
  supports_vision: boolean;
  // 只含该 provider 声明过的参数；手写进 yaml 的其余键不公布，保存时由后端保住。
  options: Record<string, unknown>;
}

// 提交回去时带上原始位置。后端靠它认出「这一条原来是哪一条」，从而保住
// 界面读不到的东西（api_key 只回读到脱敏串，手写的未知键根本看不见）。
export interface FallbackEntryUpdate {
  _origin?: number;
  provider?: string;
  model?: string | null;
  base_url?: string | null;
  api_key?: string;
  supports_vision?: boolean;
  options?: Record<string, unknown>;
}

export interface ProvidersResponse {
  active_provider: string;
  providers: Record<string, ProviderConfigPublic>;
  fallbacks: FallbackEntryPublic[];
  // 按节点的专属链。空数组表示这个节点禁用 fallback，键不存在才是跟随全局。
  node_fallbacks: Record<string, FallbackEntryPublic[]>;
  registered: string[];
  /** config/models.yaml 顶层的保留段名。渠道块和它们同层，起这些名字保存时会被拒。 */
  reserved_names: string[];
}

export interface ChannelChoice {
  /** 写进 yaml 的值。渠道名区分大小写，转小写就查不到那个块了。 */
  value: string;
  label: string;
  wire: string;
}

/**
 * provider 字段能填的两类值：已配渠道带着地址密钥模型，裸线格式只定请求怎么发。
 * 后者不能省 —— 只想换格式、地址走环境变量的节点要用它。
 */
export function channelChoices(
  resp: ProvidersResponse | null | undefined,
): { channels: ChannelChoice[]; wires: string[] } {
  const providers = resp?.providers || {};
  const named = new Set(Object.keys(providers));
  return {
    channels: Object.entries(providers).map(([name, cfg]) => ({
      value: name,
      label: cfg.label ? `${name} · ${cfg.label}` : name,
      wire: cfg.type || name.toLowerCase(),
    })),
    // 有同名块时这个名字一定被解析成渠道，再摆一个同名的线格式只是两个一模一样的选项。
    wires: (resp?.registered || []).filter((wire) => !named.has(wire)),
  };
}

// 活跃渠道的公开配置。别用 /config/openai/secret 拿这几个字段 —— 那个端点连明文
// api_key 一起返回，它是给 engine 进程取密钥用的，不该让密钥落进浏览器。
export function activeProviderConfig(
  resp: ProvidersResponse,
): { model: string; base_url: string; api_key_present: boolean } | null {
  const cfg = resp.providers?.[resp.active_provider];
  return cfg ? { model: cfg.model, base_url: cfg.base_url, api_key_present: cfg.api_key_present } : null;
}

const PROVIDER_BLOCK = {
  base_url: TEXT,
  model: TEXT,
  base_url_raw: TEXT,
  model_raw: TEXT,
  api_key_present: BOOLEAN,
  api_key_redacted: TEXT,
  supports_vision: nullable(BOOLEAN),
  type: TEXT,
  type_explicit: BOOLEAN,
  label: TEXT,
};
const FALLBACK_ENTRY = {
  provider: TEXT,
  base_url: TEXT,
  model: TEXT,
  base_url_raw: TEXT,
  model_raw: TEXT,
  api_key_present: BOOLEAN,
  api_key_redacted: TEXT,
  supports_vision: BOOLEAN,
  options: OBJECT,
};
const PROVIDERS_TABLE = {
  active_provider: TEXT,
  providers: OBJECT,
  node_fallbacks: OBJECT,
  registered: TEXT_LIST,
  reserved_names: TEXT_LIST,
};

/** 渠道表。读和写回的是同一张：缺了哪块，渠道下拉、模型继承、备选链编辑器就拿空的顶上，说成一个都没有。 */
function providersTable(what: string, value: unknown): ProvidersResponse {
  const table = shaped(what, value, PROVIDERS_TABLE);
  for (const [name, block] of Object.entries(table.providers)) shaped(what, block, PROVIDER_BLOCK, at('providers', name));
  shapedRows(what, table.fallbacks, FALLBACK_ENTRY, 'fallbacks');
  for (const [node, chain] of Object.entries(table.node_fallbacks)) shapedRows(what, chain, FALLBACK_ENTRY, at('node_fallbacks', node));
  return table as unknown as ProvidersResponse;
}

/** 写渠道表的回执。receipt 打头说清已经做完；读不懂抛 ReceiptUnreadable。 */
function providersReceipt(receipt: string, resp: Response): Promise<ProvidersResponse> {
  return receiptOf(receipt, resp, (body) => providersTable(receipt, body));
}

export async function getProviders(token: string): Promise<ProvidersResponse> {
  const what = '渠道表';
  const resp = await apiFetch('/config/providers', { token });
  return providersTable(what, await jsonOf(what, resp));
}

export interface ProviderOptionChoice {
  value: string;
  label: string;
}

export interface ProviderOptionSpec {
  key: string;
  label: string;
  kind: 'bool' | 'int' | 'float' | 'enum' | 'text';
  default: unknown;
  desc?: string;
  choices?: ProviderOptionChoice[];
  minimum?: number;
  maximum?: number;
  depends_on?: { key: string; value: unknown };
}

export type ProviderOptionsCatalog = Record<string, ProviderOptionSpec[]>;

export interface ProviderProfiles {
  /** provider → 可调参数 */
  options: ProviderOptionsCatalog;
  /** provider → 请求格式族。同族之间只换地址不会挂。 */
  wireFormats: Record<string, string>;
  /** 官方域名 → 收得下它的请求格式族。认不出的域名不在表里。 */
  hostProfiles: Record<string, string[]>;
  /** provider → 这家的模型默认收不收图片。渠道块显式配了就不看这个。 */
  defaultVision: Record<string, boolean>;
}

// 界面不硬编码各家支持什么参数、发哪种格式 —— provider 类自己声明，这里只搬运。
// 缺了的表按空表算，老后端可能还没有它；在却不是对象就报格式不对：补成空表，界面会当这几家什么都不支持。
const PROVIDER_PROFILES = {
  options: optional(OBJECT),
  wire_formats: optional(OBJECT),
  host_profiles: optional(OBJECT),
  default_vision: optional(OBJECT),
};

export async function getProviderProfiles(token: string): Promise<ProviderProfiles> {
  const what = '渠道参数清单';
  const resp = await apiFetch('/config/provider-options', { token });
  const body = shaped(what, await jsonOf(what, resp), PROVIDER_PROFILES);
  return {
    options: (body.options ?? {}) as ProviderOptionsCatalog,
    wireFormats: (body.wire_formats ?? {}) as Record<string, string>,
    hostProfiles: (body.host_profiles ?? {}) as Record<string, string[]>,
    defaultVision: (body.default_vision ?? {}) as Record<string, boolean>,
  };
}

/** 问上游这个渠道有哪些模型。base_url 传空串是「用这家默认地址」，不传是「沿用已存的」。
 *
 * slot 是发起方所在的系统槽位。带上它，后端才会拿那一槽自己存的密钥兜底。 */
export async function listUpstreamModels(
  token: string,
  name: string,
  data: { base_url?: string; api_key?: string; slot?: string },
): Promise<string[]> {
  const what = '模型列表';
  const resp = await apiFetch(`/config/providers/${encodeURIComponent(name)}/models`, {
    method: 'POST',
    token,
    json: data,
  });
  return shaped(what, await jsonOf(what, resp), { models: TEXT_LIST }).models;
}

export async function upsertProvider(
  token: string,
  name: string,
  data: {
    base_url?: string; api_key?: string; model?: string;
    supports_vision?: 'auto' | 'yes' | 'no';
    // 空串是「回去按渠道名猜」，不传才是「不动」。
    type?: string; label?: string;
  },
): Promise<ProvidersResponse> {
  const resp = await apiFetch(`/config/providers/${encodeURIComponent(name)}`, {
    method: 'PUT',
    token,
    json: data,
  });
  return providersReceipt(savedReceipt(`渠道 ${name}`), resp);
}

export async function deleteProvider(token: string, name: string): Promise<ProvidersResponse> {
  const resp = await apiFetch(`/config/providers/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    token,
  });
  return providersReceipt(nameWith(`渠道 ${name}`, '已经删掉，可回执'), resp);
}

export interface SystemModelSlot {
  key: string;
  label: string;
  desc: string;
  /** 工具子进程的请求格式写死在源码里，给它配 provider 不生效。 */
  supports_provider: boolean;
  env_prefix: string;
  model: string;
  base_url: string;
  // 展开前的原文，编辑器回填这一份。
  model_raw: string;
  base_url_raw: string;
  provider: string;
  api_key_present: boolean;
  api_key_redacted: string;
}

/** 生图工具当前能不能跑通。含主渠道回退，与工具子进程同一份判定。 */
export interface ImageToolStatus {
  name: string;
  slot: string;
  available: boolean;
}

export interface SystemModelsResponse {
  slots: SystemModelSlot[];
  image_tools?: ImageToolStatus[];
  image_default_channel?: string;
}

const SYSTEM_MODEL_SLOT = {
  key: NON_EMPTY_TEXT,
  label: TEXT,
  desc: TEXT,
  supports_provider: BOOLEAN,
  env_prefix: TEXT,
  model: TEXT,
  base_url: TEXT,
  model_raw: TEXT,
  base_url_raw: TEXT,
  provider: TEXT,
  api_key_present: BOOLEAN,
  api_key_redacted: TEXT,
};
const IMAGE_TOOL_STATUS = { name: NON_EMPTY_TEXT, slot: TEXT, available: BOOLEAN };

// 槽位补成空表，页面会说「没有可配的槽位」；model_raw 补成空串，保存一次就把变量引用抹掉了。
function systemModelsOf(what: string, value: unknown): SystemModelsResponse {
  const body = shaped(what, value, { image_default_channel: optional(TEXT) });
  const slots = shapedRows(what, body.slots, SYSTEM_MODEL_SLOT, 'slots');
  if (body.image_tools === undefined) return { ...body, slots };
  return { ...body, slots, image_tools: shapedRows(what, body.image_tools, IMAGE_TOOL_STATUS, 'image_tools') };
}

export async function getSystemModels(token: string): Promise<SystemModelsResponse> {
  const what = '系统槽位';
  const resp = await apiFetch('/config/system-models', { token });
  return systemModelsOf(what, await jsonOf(what, resp));
}

// ── Policy（服务端策略，对所有渠道生效） ──

export type PolicyDecision = 'auto' | 'approval_required' | 'deny';

export interface PolicyRule {
  pattern: string;
  decision: PolicyDecision;
  reason?: string;
}

export interface PolicyRuleSection {
  default: PolicyDecision;
  rules: PolicyRule[];
}

/** 同意自己发起的审批时，给自己授权多久免审，单位分钟。 */
export interface ApprovalGrantSettings {
  default_minutes: number;
  max_minutes: number;
  /** 管理员名单里的人能不能要永久授权；没给按开着算。 */
  permanent_for_admins?: boolean;
}

export interface PolicyDoc {
  version?: number;
  extra_roots?: string[];
  read_file?: PolicyRuleSection;
  write_file?: PolicyRuleSection;
  execute_command?: {
    default: PolicyDecision;
    deny_patterns?: string[];
    sensitive_patterns?: string[];
    sensitive_path_patterns?: string[];
  };
  restart?: { default: PolicyDecision };
  approval_grant?: ApprovalGrantSettings;
}

/** 策略连同它出自的那一版文件的指纹。 */
export interface PolicyFile {
  policy: PolicyDoc;
  hash: string | null;
}

// 补成空文档的话，页面会把每一类都摆成「没有规则」，照着存一次就把整份策略清空了。
const POLICY_FILE = { policy: OBJECT, hash: TEXT };

const policyFileOf = (what: string, data: unknown): PolicyFile => {
  const { policy, hash } = shaped(what, data, POLICY_FILE);
  return { policy: policy as PolicyDoc, hash };
};

export async function getPolicy(token: string): Promise<PolicyFile> {
  const what = '服务端策略';
  const resp = await apiFetch('/config/policy', { token });
  return policyFileOf(what, await jsonOf(what, resp));
}

/** 整份替换。部分更新没法表达「删掉一条规则」，后端也按整份校验。baseHash 是读到时的指纹，中途被别处改过就 409。
 *  存上了却读不懂回执时抛 ReceiptUnreadable。 */
export async function updatePolicy(token: string, policy: PolicyDoc, baseHash: string | null): Promise<PolicyFile> {
  const resp = await apiFetch('/config/policy', {
    method: 'PUT',
    token,
    json: baseHash === null ? { policy } : { policy, base_hash: baseHash },
  });
  const what = '策略已经存上，可回执';
  return receiptOf(what, resp, body => policyFileOf(what, body));
}

// ── bot 登录账号（经 supervisor 转发给平台的账号运维）──

/** 此前登录过、可以免扫码切回去的号。 */
export interface QuickLoginTarget {
  account: string;
  nick?: string;
  avatar?: string;
  /** 切过一次被平台拒了。平台自报的免扫码标记在这种情况下仍会给 true。 */
  available?: boolean;
  dead_reason?: string;
}

export interface AccountStatus {
  configured: boolean;
  /** 平台那头有没有应答。重启期间为 false，这不是错误。 */
  reachable?: boolean;
  reason?: string;
  account?: string;
  nick?: string;
  online?: boolean;
  is_login?: boolean;
  login_error?: string;
  /** 等扫码时会把二维码一并带出来。 */
  qrcode?: string;
  quick_login?: QuickLoginTarget[];
}

/** 一个后端实例 = 一个 bot，只跑一个平台、登一个号。多开时它们共用域名，各自挂在 path 下。 */
export interface ConsoleInstance {
  id: string;
  label: string;
  /** 挂载前缀，根实例为空串。 */
  path: string;
  current: boolean;
  /** 序号，决定端口。老清单没有这个字段时后端给 -1。 */
  idx?: number;
  /** 这个实例跑的平台 id。 */
  platform?: string;
  /** 登记的账号；还没登录过是空串。 */
  account?: string;
  provisioning?: InstanceProgress;
}

export interface InstanceDirectoryEntry {
  label: string;
  path: string;
}

export interface InstanceDirectory {
  current_path: string;
  instances: InstanceDirectoryEntry[];
}

const INSTANCE_DIRECTORY_ENTRY = { label: TEXT, path: TEXT };

export async function getInstanceDirectory(signal?: AbortSignal): Promise<InstanceDirectory> {
  const what = '实例列表';
  const response = await apiFetch('/instance-directory', {
    signal, credentials: 'omit', cache: 'no-store', anonymous: true, timeoutMs: 10_000, bypassPlugins: true,
  });
  const data = shaped(what, await jsonOf(what, response), { current_path: TEXT });
  if (data.current_path !== MOUNT) throw new Error('实例列表与当前入口不一致，请检查服务配置');
  const paths = new Set<string>();
  const instances: InstanceDirectoryEntry[] = [];
  for (const item of shapedRows(what, data.instances, INSTANCE_DIRECTORY_ENTRY, 'instances')) {
    if (!isInstancePath(item.path) || !item.label.trim()) throw new Error('实例列表包含无效地址或名称');
    if (paths.has(item.path)) continue;
    paths.add(item.path);
    instances.push({ label: item.label, path: item.path });
  }
  if (!paths.has(MOUNT)) throw new Error('实例列表缺少当前实例，请检查服务配置');
  return { current_path: MOUNT, instances };
}

/** 清单里登记着这个号的实例，本实例也算。比的是平台加账号：别的平台上可以有长得一样的号。 */
export function instanceOwning(
  platformId: string,
  account: string,
  instances: ConsoleInstance[],
): ConsoleInstance | null {
  if (!account) return null;
  return instances.find((row) => row.platform === platformId && row.account === account) ?? null;
}

/**
 * 这个号归不归别的实例管。归了就不能从本实例登过去 —— 同一个号双登，
 * 两边会互相把对方踢下线，数据也会分成两份。
 *
 * 清单为空（单实例，或读不到）时一律返回 null：宁可不拦，也不能把所有号都锁死。
 */
export function ownedByAnotherInstance(
  platformId: string,
  account: string,
  instances: ConsoleInstance[],
): ConsoleInstance | null {
  return instanceOwning(platformId, account, instances.filter((row) => !row.current));
}

const INSTANCE_PROGRESS = {
  id: NON_EMPTY_TEXT,
  lines: TEXT_LIST,
  finished: BOOLEAN,
  ok: BOOLEAN,
  detail: TEXT,
  state: optional(oneOf('queued', 'running')),
  retryable: optional(BOOLEAN),
  action: optional(oneOf('create', 'remove')),
};

const CONSOLE_INSTANCE = {
  id: NON_EMPTY_TEXT,
  label: TEXT,
  path: TEXT,
  current: BOOLEAN,
  idx: optional(NUMBER),
  platform: optional(TEXT),
  account: optional(TEXT),
  provisioning: optional(OBJECT),
};

// 读不懂就抛，不能当成单实例：清单为空时 ownedByAnotherInstance 一律放行，双登拦不住。
export async function getInstances(token: string): Promise<ConsoleInstance[]> {
  const what = '实例列表';
  const resp = await apiFetch('/instances', { token });
  const body = shaped(what, await jsonOf(what, resp), {});
  return shapedRows(what, body.instances, CONSOLE_INSTANCE, 'instances').map((row, index): ConsoleInstance => ({
    ...row,
    provisioning: row.provisioning
      && shaped(what, row.provisioning, INSTANCE_PROGRESS, at(at('instances', index), 'provisioning')),
  }));
}

/** 新实例落点。序号决定端口，前缀决定它挂在域名的哪一层。 */
export interface InstancePlan {
  id: string;
  label: string;
  idx: number;
  prefix: string;
  ports: Record<string, number>;
  workspace: string;
  platform: string;
  account: string;
}

/** root 侧建实例的实时进度。finished 之前一直轮询。 */
export interface InstanceProgress {
  id: string;
  lines: string[];
  finished: boolean;
  ok: boolean;
  detail: string;
  state?: 'queued' | 'running';
  retryable?: boolean;
  action?: 'create' | 'remove';
}

// 和后端 provisioning 同一条规则，这里只为提前提示，以后端判定为准。
const INSTANCE_ID = /^(?:[a-z][a-z0-9_]{1,31}|[1-9]\d{4,10})$/;
// 数据根下的共享表情包库和共享配置组占着这两个名字。
const RESERVED_INSTANCE_IDS: ReadonlySet<string> = new Set(['stickers', 'shared']);

export function isValidInstanceId(id: string): boolean {
  return INSTANCE_ID.test(id) && !RESERVED_INSTANCE_IDS.has(id);
}

const INSTANCE_PLAN = {
  id: NON_EMPTY_TEXT,
  label: TEXT,
  idx: NUMBER,
  prefix: TEXT,
  ports: recordOf(NUMBER),
  workspace: TEXT,
  platform: TEXT,
  account: TEXT,
};

/** 工作区建好、已经交给 root 侧接着建，只是回执读不懂时抛 ReceiptUnreadable：照填的实例名盯进度就行。 */
export async function createInstance(
  token: string,
  id: string,
  label: string,
  platform: string,
  account: string,
): Promise<InstancePlan> {
  const resp = await apiFetch('/instances', {
    method: 'POST',
    token,
    json: { id, label, platform, account },
  });
  const receipt = nameWith(`实例 ${id}`, '已经开始建，可回执');
  return receiptOf(receipt, resp, (body) => shaped(receipt, body, INSTANCE_PLAN));
}

export async function getInstanceProgress(
  token: string,
  id: string,
): Promise<InstanceProgress> {
  const what = nameWith(`实例 ${id}`, '的进度');
  const resp = await apiFetch(`/instances/${encodeURIComponent(id)}/progress`, {
    token,
  });
  return shaped(what, await jsonOf(what, resp), INSTANCE_PROGRESS);
}

export async function deleteInstance(token: string, id: string): Promise<void> {
  await apiFetch(`/instances/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    token,
  });
}

// ── 共享配置组 ──

export interface ShareGroupMember {
  id: string;
  label: string;
  current: boolean;
}

export interface ShareGroup {
  name: string;
  members: ShareGroupMember[];
  created_at: string;
  created_by: string;
}

/** 本实例能不能用共享配置、在哪个组、各组有谁、加入组时归档过哪几份旧配置。 */
export interface ShareGroupOverview {
  available: boolean;
  reason: string;
  current: string | null;
  groups: ShareGroup[];
  /** 非空 = 实例清单读不懂，查不了谁在用哪个组：这时各组的 members 是不知道，不是没人。老后端没有这一项。 */
  members_problem?: string;
  archives: string[];
}

/** 建组、加入、退出、删组的结果。archived 是本实例旧配置归档到哪，没归档是空串。 */
export interface ShareGroupChange {
  group: string;
  archived?: string;
  copied_env?: string[];
  overview: ShareGroupOverview;
}

// 和后端 config_layout 同一条规则，这里只为提前提示，以后端判定为准。
const SHARE_GROUP_NAME = /^[a-z][a-z0-9_]{1,31}$/;

export function isValidShareGroupName(name: string): boolean {
  return SHARE_GROUP_NAME.test(name);
}

const SHARE_GROUP_OVERVIEW = {
  available: BOOLEAN,
  reason: TEXT,
  current: nullable(TEXT),
  members_problem: optional(TEXT),
  archives: TEXT_LIST,
};
const SHARE_GROUP = { name: NON_EMPTY_TEXT, created_at: TEXT, created_by: TEXT };
const SHARE_GROUP_MEMBER = { id: NON_EMPTY_TEXT, label: TEXT, current: BOOLEAN };
const SHARE_GROUP_CHANGE = { group: NON_EMPTY_TEXT, archived: optional(TEXT), copied_env: optional(TEXT_LIST) };

// 回执是改完才发的：读不懂不等于没改成，报错得先说已经做完，免得人当成失败再点一次。
const SHARE_GROUP_RECEIPT = '共享配置组的操作已经做完，可回执';

function readShareGroups(what: string, raw: unknown, path = ''): ShareGroupOverview {
  const overview = shaped(what, raw, SHARE_GROUP_OVERVIEW, path);
  const groupsPath = at(path, 'groups');
  return {
    available: overview.available,
    reason: overview.reason,
    current: overview.current || null,
    groups: shapedRows(what, overview.groups, SHARE_GROUP, groupsPath).map((group, index) => ({
      name: group.name,
      members: shapedRows(what, group.members, SHARE_GROUP_MEMBER, at(at(groupsPath, index), 'members'))
        .map(({ id, label, current }) => ({ id, label, current })),
      created_at: group.created_at,
      created_by: group.created_by,
    })),
    members_problem: overview.members_problem,
    archives: overview.archives,
  };
}

/** 后端拒绝时照常抛 ApiError；改完了却读不懂回执时抛 ReceiptUnreadable。 */
async function changeShareGroup(path: string, token: string, init: RequestOptions): Promise<ShareGroupChange> {
  const resp = await apiFetch(path, { ...init, token });
  return receiptOf(SHARE_GROUP_RECEIPT, resp, (body) => {
    const change = shaped(SHARE_GROUP_RECEIPT, body, SHARE_GROUP_CHANGE);
    return {
      group: change.group,
      archived: change.archived ?? '',
      copied_env: change.copied_env ?? [],
      overview: readShareGroups(SHARE_GROUP_RECEIPT, change.overview, 'overview'),
    };
  });
}

export async function getShareGroups(token: string): Promise<ShareGroupOverview> {
  const what = '共享配置组';
  const resp = await apiFetch('/share-groups', { token });
  return readShareGroups(what, await jsonOf(what, resp));
}

export function createShareGroup(token: string, name: string): Promise<ShareGroupChange> {
  return changeShareGroup('/share-groups', token, { method: 'POST', json: { name } });
}

export function joinShareGroup(token: string, name: string): Promise<ShareGroupChange> {
  return changeShareGroup(`/share-groups/${encodeURIComponent(name)}/join`, token, { method: 'POST', json: {} });
}

export function leaveShareGroup(token: string): Promise<ShareGroupChange> {
  return changeShareGroup('/share-groups/leave', token, { method: 'POST', json: {} });
}

export function deleteShareGroup(token: string, name: string): Promise<ShareGroupChange> {
  return changeShareGroup(`/share-groups/${encodeURIComponent(name)}`, token, { method: 'DELETE' });
}

const platformAccountPath = (platformId: string, leaf = ''): string =>
  `/platforms/${encodeURIComponent(platformId)}/account${leaf}`;

const QUICK_LOGIN_TARGET = {
  account: NON_EMPTY_TEXT,
  nick: optional(TEXT),
  avatar: optional(TEXT),
  available: optional(BOOLEAN),
  dead_reason: optional(TEXT),
};
const ACCOUNT_STATUS = {
  configured: BOOLEAN,
  reachable: optional(BOOLEAN),
  reason: optional(TEXT),
  account: optional(TEXT),
  nick: optional(TEXT),
  online: optional(BOOLEAN),
  is_login: optional(BOOLEAN),
  login_error: optional(TEXT),
  qrcode: optional(TEXT),
};

// 读不懂不能当成没配：页面会改口叫人去配 WebUI token，可那份配置本来没问题。
export async function getPlatformAccount(token: string, platformId: string): Promise<AccountStatus> {
  const what = '账号状态';
  const resp = await apiFetch(platformAccountPath(platformId), { token });
  const status = shaped(what, await jsonOf(what, resp), ACCOUNT_STATUS);
  return status.quick_login === undefined
    ? status
    : { ...status, quick_login: shapedRows(what, status.quick_login, QUICK_LOGIN_TARGET, 'quick_login') };
}

/** 换号、钉号的回执。 */
export interface AccountChange {
  /** 号已经换上，但没记进实例清单（多开时数据根下的清单写不了）时的原因；记上了是空串。 */
  rosterError: string;
}

const ACCOUNT_CHANGE = { ok: BOOLEAN, account: TEXT, roster_error: optional(TEXT) };

// 号已经换上、只是回执读不懂时抛 ReceiptUnreadable：说成没换上，人会再换一次。
async function accountChangeOf(receipt: string, resp: Response, refused: string): Promise<AccountChange> {
  const change = await receiptOf(receipt, resp, (body) => shaped(receipt, body, ACCOUNT_CHANGE));
  if (!change.ok) throw new Error(refused);
  return { rosterError: change.roster_error?.trim() ?? '' };
}

export async function switchPlatformAccount(token: string, platformId: string, account: string): Promise<AccountChange> {
  const resp = await apiFetch(platformAccountPath(platformId, '/switch'), {
    method: 'POST',
    token,
    json: { account },
  });
  return accountChangeOf(`已经切到 ${account}，可回执`, resp, `服务端没有切到 ${account}，请重试`);
}

/** 让平台停在等扫码的状态。QQ 在已登录状态下拒发二维码、也没有登出接口，只能清掉自动登录再重启容器。 */
export async function enterPlatformLoginMode(token: string, platformId: string): Promise<void> {
  await apiFetch(platformAccountPath(platformId, '/relogin'), {
    method: 'POST',
    token,
    json: {},
  });
}

/** 把当前在线的号钉成自动登录，否则下次重启会停在等扫码。 */
export async function pinPlatformAccount(token: string, platformId: string): Promise<AccountChange> {
  const resp = await apiFetch(platformAccountPath(platformId, '/pin'), {
    method: 'POST',
    token,
    json: {},
  });
  return accountChangeOf('已经设成重启后自动登录，可回执', resp, '服务端没有设成重启后自动登录，请重试');
}

export interface LoginQrcode {
  qrcode: string;
  /** false 表示平台那头还没起来，等下一轮即可，不必当成失败。 */
  reachable: boolean;
  reason?: string;
}

const LOGIN_QRCODE = { qrcode: TEXT, reachable: BOOLEAN, reason: optional(TEXT) };

export async function getPlatformLoginQrcode(token: string, platformId: string): Promise<LoginQrcode> {
  const what = '登录二维码';
  const resp = await apiFetch(platformAccountPath(platformId, '/qrcode'), {
    method: 'POST',
    token,
    json: {},
  });
  return shaped(what, await jsonOf(what, resp), LOGIN_QRCODE);
}

/** 字段留空串表示删掉这一项（回到跟随主渠道）；不传表示不动。 */
export async function updateSystemModel(
  token: string,
  slot: string,
  data: { base_url?: string; api_key?: string; model?: string; provider?: string },
): Promise<SystemModelsResponse> {
  const resp = await apiFetch(`/config/system-models/${encodeURIComponent(slot)}`, {
    method: 'PUT',
    token,
    json: data,
  });
  const receipt = savedReceipt(`系统槽位 ${slot}`);
  return receiptOf(receipt, resp, (body) => systemModelsOf(receipt, body));
}

/** 配了多个生图渠道时默认用哪个。空串 = 交给模型按用途判断。存上了却读不懂回执时抛 ReceiptUnreadable。 */
export async function updateImageDefaultChannel(
  token: string,
  defaultChannel: string,
): Promise<SystemModelsResponse> {
  const resp = await apiFetch('/config/image-default-channel', {
    method: 'PUT',
    token,
    json: { default_channel: defaultChannel },
  });
  const receipt = savedReceipt('默认生图渠道');
  return receiptOf(receipt, resp, (body) => systemModelsOf(receipt, body));
}

export async function setActiveProvider(token: string, provider: string): Promise<ProvidersResponse> {
  const resp = await apiFetch('/config/active-provider', {
    method: 'PUT',
    token,
    json: { provider },
  });
  return providersReceipt(`已经切到渠道 ${provider}，可回执`, resp);
}

export async function updateFallbacks(
  token: string,
  fallbacks: FallbackEntryUpdate[],
): Promise<ProvidersResponse> {
  const resp = await apiFetch('/config/fallbacks', {
    method: 'PUT',
    token,
    json: { fallbacks },
  });
  return providersReceipt(savedReceipt('全局备选链'), resp);
}

export async function updateNodeFallbacks(
  token: string,
  nodeId: string,
  fallbacks: FallbackEntryUpdate[],
): Promise<ProvidersResponse> {
  const resp = await apiFetch(`/config/node-fallbacks/${encodeURIComponent(nodeId)}`, {
    method: 'PUT',
    token,
    json: { fallbacks },
  });
  return providersReceipt(savedReceipt(`节点 ${nodeId} 的备选链`), resp);
}

// 删掉专属链 = 这个节点回退到全局链。提交空数组是「禁用」，两者不同。
export async function clearNodeFallbacks(
  token: string,
  nodeId: string,
): Promise<ProvidersResponse> {
  const resp = await apiFetch(`/config/node-fallbacks/${encodeURIComponent(nodeId)}`, {
    method: 'DELETE',
    token,
  });
  return providersReceipt(`节点 ${nodeId} 已经改回跟随全局链，可回执`, resp);
}

export interface RuntimeFile extends RawText {
  /** default：本实例还没改过，显示的是仓库默认；override：本实例有自己的 config/runtime.yaml，逐键盖在默认上面。 */
  source: 'default' | 'override';
  path: string;
}

/** runtime.yaml 连同表单要显示的两层。两层都由后端照运行时那套合并算好，这边不另合一遍。 */
export interface RuntimeLayers extends RuntimeFile {
  /** 本实例没写的键跟着的那一层：代码里的默认值 ← 仓库的 defaults/runtime.yaml。 */
  defaults: Record<string, unknown>;
  /** 实际在用的：默认那层上面逐键盖着本实例那份；那份写坏时是还在用的上一份。 */
  effective: Record<string, unknown>;
}

// 来源认不出时不能当成仓库默认：结构化表单会从空文档写起，本实例那份里别的键就丢了。
const RUNTIME_FILE = { ...RAW_TEXT, source: oneOf('default', 'override'), path: TEXT };
const RUNTIME_LAYERS = { ...RUNTIME_FILE, defaults: OBJECT, effective: OBJECT };

export async function getRuntimeFile(token: string): Promise<RuntimeFile> {
  const what = 'runtime.yaml 原文';
  const resp = await apiFetch('/admin/config/runtime/raw', { token });
  const { content, hash, source, path } = shaped(what, await jsonOf(what, resp), RUNTIME_FILE);
  return { content, hash, source, path };
}

export async function getRuntimeLayers(token: string): Promise<RuntimeLayers> {
  const what = 'runtime.yaml 原文';
  const resp = await apiFetch('/admin/config/runtime/raw', { token });
  const { content, hash, source, path, defaults, effective } = shaped(what, await jsonOf(what, resp), RUNTIME_LAYERS);
  return { content, hash, source, path, defaults, effective };
}

/** 删掉本实例的 runtime.yaml，全部参数回到仓库默认。删了却读不懂回执时抛 ReceiptUnreadable。 */
export async function resetRuntime(token: string): Promise<ConfigRemoveResult> {
  const resp = await apiFetch('/admin/config/runtime/raw', {
    method: 'DELETE',
    token,
  });
  return removeResultOf(removedReceipt('runtime.yaml'), resp);
}

export function updateRuntimeRaw(token: string, yaml: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig('runtime.yaml', '/admin/config/runtime/raw', token, yaml, baseHash);
}

export function getPolicyRaw(token: string): Promise<RawText> {
  return readRawConfig('policy.yaml 原文', '/admin/config/policy/raw', token);
}

export function updatePolicyRaw(token: string, yaml: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig('policy.yaml', '/admin/config/policy/raw', token, yaml, baseHash);
}

// ── Memory & conversation context ──

export interface MemoryOwner {
  kind: 'group' | 'private' | 'agent' | 'subject' | 'unknown';
  label: string;
  name_available?: boolean;
  alias?: string;
  real_id?: string;
  conversation_key?: string;
}

export interface MemoryNamespace {
  key: string;
  namespace: string;
  /** memory_book：节点在 memory_book 里自己命名的记忆库，不属于任何会话。 */
  kind: 'conversation' | 'subject' | 'memory_book' | 'orphan';
  subject: string;
  /** 读得出来的条数，不含 unreadable_books 里那几本。 */
  entry_count: number;
  books: string[];
  /** 读不出来的记忆本名。老后端没有这一项，那时读不出来的本是悄悄跳过的。 */
  unreadable_books: string[];
  updated_at: string;
  owner: MemoryOwner;
}

/** 后端只核了 book 和 constant。id、content、keywords 是记忆本里手写的原样，不一定是字符串：显示时照注入时的读法认。 */
export interface MemoryEntry {
  id: unknown;
  book: string;
  content: unknown;
  keywords?: unknown;
  constant?: boolean;
  enabled?: boolean;
  priority?: number;
  scan_depth?: number;
  subject?: string;
  source?: string;
  created_at?: string;
  updated_at?: string;
}

export const MEMORY_OWNER = { kind: TEXT, label: TEXT };

/** 读不出来的一本记忆。problem 只有位置和问题，不带文件内容。 */
export interface UnreadableMemoryBook {
  book: string;
  problem: string;
}

export interface MemoryEntries {
  entries: MemoryEntry[];
  /** 这几本读不出来，里面的条目不在 entries 里。 */
  unreadable_books: UnreadableMemoryBook[];
}

const MEMORY_NAMESPACE = {
  key: NON_EMPTY_TEXT,
  namespace: TEXT,
  kind: TEXT,
  subject: TEXT,
  entry_count: NUMBER,
  books: TEXT_LIST,
  unreadable_books: optional(TEXT_LIST),
  updated_at: TEXT,
};

// 条目的其余字段是记忆本里手写的原样，只核后端自己填的那几项。
const MEMORY_ENTRY = { book: NON_EMPTY_TEXT, constant: BOOLEAN };

const UNREADABLE_MEMORY_BOOK = { book: NON_EMPTY_TEXT, problem: TEXT };

// 读不懂就抛：补成空表，页面会说「还没有任何记忆」「0 条」。
export async function getMemoryOverview(token: string): Promise<MemoryNamespace[]> {
  const what = '记忆来源列表';
  const resp = await apiFetch('/admin/config/memory/overview', { token });
  const body = shaped(what, await jsonOf(what, resp), {});
  return shapedRows(what, body.namespaces, MEMORY_NAMESPACE, 'namespaces').map((row, index) => ({
    ...row,
    unreadable_books: row.unreadable_books ?? [],
    owner: shaped(what, row.owner, MEMORY_OWNER, at(at('namespaces', index), 'owner')),
  })) as unknown as MemoryNamespace[];
}

export async function getMemoryEntries(token: string, namespace: string): Promise<MemoryEntries> {
  const what = '记忆条目列表';
  const resp = await apiFetch(
    `/admin/config/memory/${encodeURIComponent(namespace)}/entries`, { token },
  );
  const body = shaped(what, await jsonOf(what, resp), {});
  const unreadable = body.unreadable_books === undefined
    ? []
    : shapedRows(what, body.unreadable_books, UNREADABLE_MEMORY_BOOK, 'unreadable_books');
  return {
    entries: shapedRows(what, body.entries, MEMORY_ENTRY, 'entries') as unknown as MemoryEntry[],
    unreadable_books: unreadable.map(({ book, problem }) => ({ book, problem })),
  };
}

const MEMORY_SAVE = { ok: BOOLEAN, entry: OBJECT };

/** 存上了却读不懂回执时抛 ReceiptUnreadable：再存一次会把同一条整条覆盖一遍。 */
export async function saveMemoryEntry(
  token: string, namespace: string, entry: Partial<MemoryEntry>,
): Promise<MemoryEntry> {
  const resp = await apiFetch(`/admin/config/memory/${encodeURIComponent(namespace)}/entries`, {
    method: 'POST',
    token,
    json: entry,
  });
  const receipt = savedReceipt('这条记忆');
  const saved = await receiptOf(receipt, resp, (body) => {
    const result = shaped(receipt, body, MEMORY_SAVE);
    return { ok: result.ok, entry: shaped(receipt, result.entry, MEMORY_ENTRY, 'entry') as unknown as MemoryEntry };
  });
  if (!saved.ok) throw new Error('服务端没有存上这条记忆，请重试');
  return saved.entry;
}

export async function deleteMemoryEntry(
  token: string, namespace: string, book: string, entryId: string,
): Promise<void> {
  await apiFetch(
    `/admin/config/memory/${encodeURIComponent(namespace)}/entries/${encodeURIComponent(book)}/${encodeURIComponent(entryId)}`,
    { method: 'DELETE', token },
  );
}

const MEMORY_CLEAR = { ok: BOOLEAN, removed: NUMBER };

/** 清空了却读不懂回执时抛 ReceiptUnreadable：记忆已经删了，只是不知道删了几条。 */
export async function clearMemoryNamespace(token: string, namespace: string): Promise<number> {
  const resp = await apiFetch(`/admin/config/memory/${encodeURIComponent(namespace)}`, {
    method: 'DELETE', token,
  });
  const receipt = '这一份记忆已经清空，可回执';
  const result = await receiptOf(receipt, resp, (body) => shaped(receipt, body, MEMORY_CLEAR));
  if (!result.ok) throw new Error('服务端没有清空这一份记忆，请重试');
  return result.removed;
}

export interface ConversationRow {
  session_id: string;
  conversation_key: string;
  channel: string;
  bytes: number;
  updated_at: number;
  owner: MemoryOwner | null;
  /** 属于当前登录的 QQ 号。换号后旧号的会话仍在磁盘上，但不该跟当前的混在一起。 */
  current_account?: boolean;
}

export interface MaterialScope {
  scope: string;
  bot_scope: string;
  owner?: MemoryOwner | null;
  current_account?: boolean;
}

const CONVERSATION_ROW = {
  session_id: NON_EMPTY_TEXT,
  conversation_key: TEXT,
  channel: TEXT,
  bytes: NUMBER,
  updated_at: NUMBER,
  owner: nullable(OBJECT),
  current_account: optional(BOOLEAN),
};

export async function getConversations(token: string): Promise<ConversationRow[]> {
  const what = '会话列表';
  const resp = await apiFetch('/admin/conversations', { token });
  const body = shaped(what, await jsonOf(what, resp), {});
  return shapedRows(what, body.conversations, CONVERSATION_ROW, 'conversations').map((row, index) => ({
    ...row,
    owner: row.owner === null ? null : shaped(what, row.owner, MEMORY_OWNER, at(at('conversations', index), 'owner')),
  })) as unknown as ConversationRow[];
}

/** 存档里的一条消息。tool_calls、meta 这些照存档原样带着，预览只念这两项。 */
export interface ConversationMessage {
  role: string;
  content: MessageContent;
}

export interface ConversationMessages {
  /** 存档里一共几条；messages 只是最近的 limit 条。 */
  total: number;
  messages: ConversationMessage[];
}

export async function getConversationMessages(
  token: string, sessionId: string, limit = 200,
): Promise<ConversationMessages> {
  const what = '会话记录';
  const resp = await apiFetch(
    `/admin/conversations/${encodeURIComponent(sessionId)}/messages?limit=${limit}`,
    { token },
  );
  const body = shaped(what, await jsonOf(what, resp), { total: NUMBER });
  return {
    total: body.total,
    messages: shapedRows(what, body.messages, { role: TEXT, content: MESSAGE_CONTENT }, 'messages'),
  };
}

/** 删掉了却读不懂回执时抛 ReceiptUnreadable。 */
export async function resetConversationBySession(token: string, sessionId: string): Promise<ConversationReset> {
  const resp = await apiFetch(`/admin/conversations/${encodeURIComponent(sessionId)}/reset`, {
    method: 'POST', token,
  });
  return conversationResetOf('会话上下文已经删掉，可回执', resp);
}

export interface ScopePlanRow {
  old_namespace: string;
  new_namespace: string;
  has_memory: boolean;
  /** 两边都有长期记忆：整段会话原样留着，不搬。 */
  blocked: boolean;
}

/** 一个账号名下有几个会话。scope 为空串是加账号隔离之前、没有归属的存量。 */
export interface ScopeCount {
  scope: string;
  conversations: number;
}

/** 一个按会话键存数据的库会改写几行；replaced 是撞上目标已有数据、被顶掉的行。 */
export interface ScopeStoreCount {
  label: string;
  moved: number;
  replaced: number;
}

export interface ScopeStatus {
  current_scope: string;
  live_scope: string;
  bot_alive: boolean;
  preview: {
    source_scope: string;
    target_scope: string;
    conversations: ScopePlanRow[];
    unknown_namespaces: string[];
    /** 路由表里有、但认不出是在哪个账号下算的会话。 */
    unresolved_namespaces: string[];
    /** 源和目标以外的账号名下的会话，不在这次搬迁里。 */
    other_scopes: ScopeCount[];
    stores: Record<string, ScopeStoreCount>;
  } | null;
  /** 目标以外有几个账号名下都有会话、认不出该搬哪批时列出来，挑一个带着 source 重新预览。 */
  ambiguous_sources: ScopeCount[];
}

const SCOPE_COUNT = { scope: TEXT, conversations: NUMBER };
const SCOPE_PLAN_ROW = { old_namespace: NON_EMPTY_TEXT, new_namespace: TEXT, has_memory: BOOLEAN, blocked: BOOLEAN };
const SCOPE_STORE_COUNT = { label: TEXT, moved: NUMBER, replaced: NUMBER };
const SCOPE_STATUS = { current_scope: TEXT, live_scope: TEXT, bot_alive: BOOLEAN, preview: nullable(OBJECT) };
const SCOPE_PREVIEW = {
  source_scope: TEXT,
  target_scope: TEXT,
  unknown_namespaces: TEXT_LIST,
  unresolved_namespaces: TEXT_LIST,
};

// 「还没绑定账号」「无需搬迁」都是读到以后才说得出的结论，读不懂就抛。
function scopeStatusOf(what: string, value: unknown): ScopeStatus {
  const status = shaped(what, value, SCOPE_STATUS);
  const ambiguousSources = shapedRows(what, status.ambiguous_sources, SCOPE_COUNT, 'ambiguous_sources');
  if (status.preview === null) return { ...status, preview: null, ambiguous_sources: ambiguousSources };
  const preview = shaped(what, status.preview, SCOPE_PREVIEW, 'preview');
  return {
    ...status,
    ambiguous_sources: ambiguousSources,
    preview: {
      ...preview,
      conversations: shapedRows(what, preview.conversations, SCOPE_PLAN_ROW, 'preview.conversations'),
      other_scopes: shapedRows(what, preview.other_scopes, SCOPE_COUNT, 'preview.other_scopes'),
      stores: shapedRecord(what, preview.stores, SCOPE_STORE_COUNT, 'preview.stores'),
    },
  };
}

export async function getAccountScope(
  token: string,
  platformId: string,
  target = '',
  source?: string,
): Promise<ScopeStatus> {
  const params = new URLSearchParams();
  if (target) params.set('target', target);
  // 空串是「没有归属的存量」，和不传是两回事。
  if (source !== undefined) params.set('source', source);
  const query = params.toString();
  const what = '记忆归属';
  const resp = await apiFetch(`${platformAccountPath(platformId, '/scope')}${query ? `?${query}` : ''}`, {
    token,
  });
  return scopeStatusOf(what, await jsonOf(what, resp));
}

/** 搬迁的回执。后端还报各库改写的行数和跳过的会话，页面只念这几项。 */
export interface ScopeMigration {
  ok: boolean;
  moved_memory_dirs: number;
  sessions_changed: number;
  backup_dir: string;
  /** 搬了记忆、命中记录却读不了没跟着改名时的原因；没出事是空串。 */
  hit_cache_problem: string;
}

const SCOPE_MIGRATION = {
  ok: BOOLEAN,
  moved_memory_dirs: NUMBER,
  sessions_changed: NUMBER,
  backup_dir: TEXT,
  hit_cache_problem: TEXT,
};

/** 搬完了却读不懂回执时抛 ReceiptUnreadable：数据已经划过去了，再搬一次只会扑空。 */
export async function migrateAccountScope(
  token: string,
  platformId: string,
  target: string,
  source?: string,
): Promise<ScopeMigration> {
  const resp = await apiFetch(platformAccountPath(platformId, '/scope/migrate'), {
    method: 'POST',
    token,
    json: source === undefined ? { target } : { target, source },
  });
  const receipt = '记忆归属已经搬完，可回执';
  const result = await receiptOf(receipt, resp, (body) => shaped(receipt, body, SCOPE_MIGRATION));
  if (!result.ok) throw new Error('服务端没有搬迁，请重试');
  return result;
}

// ── Sticker library ──

export type StickerState = 'pending' | 'library' | 'discarded';

export interface Sticker {
  sha256: string;
  name: string;
  state: StickerState;
  source: string;
  /** auto_tags 与 manual_tags 合并后的检索用标签，由后端算好。 */
  tags: string[];
  auto_tags: string[];
  manual_tags: string[];
  manual_override: boolean;
  caption_state: 'pending' | 'running' | 'done' | 'failed';
  caption_error: string;
  width: number;
  height: number;
  animated: boolean;
  size: number;
  from_group: string;
  from_user: string;
  sent_count: number;
  last_sent_at: number;
  created_at: number;
  usable: boolean;
}

export interface StickerCounts {
  pending: number;
  library: number;
  discarded: number;
  usable: number;
  awaiting_caption: number;
}

export interface StickerPage {
  items: Sticker[];
  counts: StickerCounts;
}

export interface StickerImportResult {
  scanned: number;
  added: number;
  known: number;
  skipped: number;
}

const STICKER = {
  sha256: NON_EMPTY_TEXT,
  name: TEXT,
  state: oneOf('pending', 'library', 'discarded'),
  source: TEXT,
  tags: TEXT_LIST,
  auto_tags: TEXT_LIST,
  manual_tags: TEXT_LIST,
  manual_override: BOOLEAN,
  caption_state: oneOf('pending', 'running', 'done', 'failed'),
  caption_error: TEXT,
  width: NUMBER,
  height: NUMBER,
  animated: BOOLEAN,
  size: NUMBER,
  from_group: TEXT,
  from_user: TEXT,
  sent_count: NUMBER,
  last_sent_at: NUMBER,
  created_at: NUMBER,
  usable: BOOLEAN,
};
const STICKER_COUNTS = { pending: NUMBER, library: NUMBER, discarded: NUMBER, usable: NUMBER, awaiting_caption: NUMBER };
const STICKER_IMPORT = { scanned: NUMBER, added: NUMBER, known: NUMBER, skipped: NUMBER };

// 改完紧接着这张图被删了，后端回的是空对象：交回 null，不当成读不懂。
function stickerReceipt(receipt: string, body: unknown): Sticker | null {
  if (isJsonObject(body) && Object.keys(body).length === 0) return null;
  return shaped(receipt, body, STICKER);
}

/** 预览图地址。带令牌取回再显示，令牌不进地址。 */
export function stickerImageUrl(sha256: string): string {
  return `${API}/stickers/${encodeURIComponent(sha256)}/image`;
}

export async function listStickers(
  token: string,
  params: { state?: string; search?: string; limit?: number; offset?: number } = {},
): Promise<StickerPage> {
  const query = new URLSearchParams({
    state: params.state || '',
    search: params.search || '',
    limit: String(params.limit ?? 60),
    offset: String(params.offset ?? 0),
  });
  const what = '图库';
  const resp = await apiFetch(`/stickers?${query}`, { token });
  // 补成空表、0 张的话，页面会说「这一档还没有图」「在库 0 张」。
  const body = shaped(what, await jsonOf(what, resp), {});
  return {
    items: shapedRows(what, body.items, STICKER, 'items'),
    counts: shaped(what, body.counts, STICKER_COUNTS, 'counts'),
  };
}

/** null：改完紧接着这张图被删了。改了却读不懂回执时抛 ReceiptUnreadable。 */
export async function updateSticker(
  token: string,
  sha256: string,
  patch: { name?: string; manual_tags?: string[]; manual_override?: boolean },
): Promise<Sticker | null> {
  const resp = await apiFetch(`/stickers/${encodeURIComponent(sha256)}`, {
    method: 'PATCH',
    token,
    json: patch,
  });
  const receipt = savedReceipt('这张图的名字和标签');
  return receiptOf(receipt, resp, (body) => stickerReceipt(receipt, body));
}

/** null：转正后紧接着这张图被删了。转了却读不懂回执时抛 ReceiptUnreadable。 */
export async function acceptSticker(token: string, sha256: string): Promise<Sticker | null> {
  const resp = await apiFetch(`/stickers/${encodeURIComponent(sha256)}/accept`, {
    method: 'POST', token,
  });
  const receipt = '这张图已经转正，可回执';
  return receiptOf(receipt, resp, (body) => stickerReceipt(receipt, body));
}

export async function discardSticker(token: string, sha256: string): Promise<void> {
  await apiFetch(`/stickers/${encodeURIComponent(sha256)}/discard`, {
    method: 'POST', token,
  });
}

export async function deleteSticker(token: string, sha256: string): Promise<void> {
  await apiFetch(`/stickers/${encodeURIComponent(sha256)}`, {
    method: 'DELETE', token,
  });
}

/** 排进队列了却读不懂回执时抛 ReceiptUnreadable：不知道排了几张，不能说成 0 张。 */
export async function recaptionStickers(token: string, onlyFailed: boolean): Promise<number> {
  const resp = await apiFetch('/stickers/recaption', {
    method: 'POST',
    token,
    json: { only_failed: onlyFailed },
  });
  const receipt = '已经重新排进打标队列，可回执';
  const result = await receiptOf(receipt, resp, (body) => shaped(receipt, body, { queued: NUMBER }));
  return result.queued;
}

export async function uploadSticker(
  token: string,
  file: File,
  options: { name?: string; accept?: boolean } = {},
): Promise<Sticker> {
  const form = new FormData();
  form.append('file', file);
  const query = new URLSearchParams({
    name: options.name || '',
    accept: String(options.accept ?? true),
  });
  // 不手动设 Content-Type：浏览器要自己填 multipart boundary。
  const resp = await apiFetch(`/stickers/upload?${query}`, {
    method: 'POST', body: form, token,
  });
  // 传上了却读不懂回执时抛 ReceiptUnreadable：再传一次会被当成库里已有的图拒掉。
  const receipt = '这张图已经传上，可回执';
  return receiptOf(receipt, resp, (body) => shaped(receipt, body, STICKER));
}

/** 导入了却读不懂回执时抛 ReceiptUnreadable。 */
export async function importStickers(
  token: string, path: string, accept: boolean,
): Promise<StickerImportResult> {
  const resp = await apiFetch('/stickers/import', {
    method: 'POST',
    token,
    json: { path, accept },
  });
  const receipt = '目录已经导入，可回执';
  return receiptOf(receipt, resp, (body) => shaped(receipt, body, STICKER_IMPORT));
}

export function getSchedulesRaw(token: string): Promise<RawText> {
  return readRawConfig('schedules.yaml 原文', '/admin/config/schedules/raw', token);
}

export function updateSchedulesRaw(token: string, yaml: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig('schedules.yaml', '/admin/config/schedules/raw', token, yaml, baseHash);
}

export interface NodeFileInfo {
  name: string;
  path: string;
  kind: 'node' | 'fragment';
  suffix: string;
  is_example: boolean;
  base_name: string;
  size: number;
  updated_at: number;
  source: Exclude<OverlaySource, 'system'>;
  same_as_default: boolean;
}

const NODE_FILE = {
  name: NON_EMPTY_TEXT,
  path: TEXT,
  kind: oneOf('node', 'fragment'),
  suffix: TEXT,
  is_example: BOOLEAN,
  base_name: TEXT,
  size: NUMBER,
  updated_at: NUMBER,
  source: oneOf('default', 'override', 'custom'),
  same_as_default: BOOLEAN,
};

export async function getNodeFiles(token: string): Promise<NodeFileInfo[]> {
  const what = '节点文件列表';
  const resp = await apiFetch('/admin/config/node-files', { token });
  return shapedRows(what, await jsonOf(what, resp), NODE_FILE);
}

export function getNodeFileRaw(token: string, filename: string): Promise<RawText> {
  return readRawConfig(`节点文件 ${filename} 的原文`, `/admin/config/node-files/${pathPart(filename)}/raw`, token);
}

export function updateNodeFileRaw(token: string, filename: string, content: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig(`节点文件 ${filename}`, `/admin/config/node-files/${pathPart(filename)}/raw`, token, content, baseHash);
}

/** 改过的仓库默认文件恢复成默认；本实例自己加的直接删。没改过的仓库默认删不掉，后端回 409。 */
export async function deleteNodeFile(token: string, filename: string): Promise<ConfigRemoveResult> {
  const resp = await apiFetch(`/admin/config/node-files/${pathPart(filename)}`, {
    method: 'DELETE',
    token,
  });
  return removeResultOf(removedReceipt(`节点文件 ${filename}`), resp);
}

export function getNodeRaw(token: string, nodeId: string): Promise<RawText> {
  return readRawConfig(`节点 ${nodeId} 的原文`, `/admin/config/nodes/${pathPart(nodeId)}/raw`, token);
}

export function updateNodeRaw(token: string, nodeId: string, yaml: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig(`节点 ${nodeId}`, `/admin/config/nodes/${pathPart(nodeId)}/raw`, token, yaml, baseHash);
}

export async function createNode(token: string, data: AdminCreatePayload): Promise<RawSaveResult> {
  const resp = await apiFetch('/admin/config/nodes', {
    method: 'POST',
    token,
    json: data,
  });
  return saveResultOf(createdReceipt(`节点 ${data.id}`), resp);
}

export async function deleteNode(token: string, nodeId: string): Promise<ConfigRemoveResult> {
  const resp = await apiFetch(`/admin/config/nodes/${pathPart(nodeId)}`, {
    method: 'DELETE',
    token,
  });
  return removeResultOf(removedReceipt(`节点 ${nodeId}`), resp);
}

const ADMIN_TOOL = {
  name: NON_EMPTY_TEXT,
  source: oneOf('builtin', 'plugin', 'external'),
  editable: BOOLEAN,
  file: optional(TEXT),
  description: optional(TEXT),
  input_schema: optional(OBJECT),
  timeout_sec: optional(NUMBER),
  has_spec: optional(BOOLEAN),
  origin: optional(oneOf('custom', 'override', 'bundled')),
};

// 读不懂就抛：补成空表的话，页面会说「共 0 个」「没有符合条件的工具」。
export async function getTools(token: string): Promise<AdminTool[]> {
  const what = '工具清单';
  const resp = await apiFetch('/admin/config/tools', { token });
  return shapedRows(what, await jsonOf(what, resp), ADMIN_TOOL);
}

export function getToolRaw(token: string, name: string): Promise<RawText> {
  return readRawConfig(`工具 ${name} 的源码`, `/admin/config/tools/${pathPart(name)}/raw`, token);
}

export function updateToolRaw(token: string, name: string, script: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig(`工具 ${name}`, `/admin/config/tools/${pathPart(name)}/raw`, token, script, baseHash);
}

export async function createTool(token: string, data: AdminCreatePayload): Promise<RawSaveResult> {
  const resp = await apiFetch('/admin/config/tools', {
    method: 'POST',
    token,
    json: data,
  });
  return saveResultOf(createdReceipt(`工具 ${data.id}`), resp);
}

export async function deleteTool(token: string, name: string): Promise<ConfigRemoveResult> {
  const resp = await apiFetch(`/admin/config/tools/${pathPart(name)}`, {
    method: 'DELETE',
    token,
  });
  return removeResultOf(removedReceipt(`工具 ${name}`), resp);
}

// ── Drawtools ──

export interface DrawtoolsRawFile {
  content: string;
  /** 读到时的指纹，保存时交回；两份都不存在时是空串。 */
  hash?: string | null;
  exists?: boolean;
  using_example?: boolean;
  path?: string;
  example_path?: string;
}

export interface DrawtoolsBundle {
  settings: DrawtoolsRawFile;
  character_tags: DrawtoolsRawFile;
  prompts: Record<string, DrawtoolsRawFile>;
}

const DRAWTOOLS_FILE = {
  ...RAW_TEXT,
  exists: BOOLEAN,
  using_example: BOOLEAN,
  path: TEXT,
  example_path: optional(TEXT),
};

// 和原文编辑器一样：哪一份的正文或指纹对不上形状就整包报格式不对，不拿空串、null 顶上。
export async function getDrawtoolsBundle(token: string): Promise<DrawtoolsBundle> {
  const what = '绘图配置';
  const resp = await apiFetch('/admin/config/drawtools', { token });
  const body = shaped(what, await jsonOf(what, resp), { prompts: OBJECT });
  const fileAt = (path: string, value: unknown): DrawtoolsRawFile => shaped(what, value, DRAWTOOLS_FILE, path);
  return {
    settings: fileAt('settings', body.settings),
    character_tags: fileAt('character_tags', body.character_tags),
    prompts: Object.fromEntries(
      Object.entries(body.prompts).map(([name, value]) => [name, fileAt(at('prompts', name), value)]),
    ),
  };
}

/** 补建绘图配置的结果：created 是这次从模板抄进来的那几份，已有的不动。 */
export interface DrawtoolsInit {
  ok: boolean;
  created: string[];
}

const DRAWTOOLS_INIT = { ok: BOOLEAN, created: TEXT_LIST };

/** 补建了却读不懂回执时抛 ReceiptUnreadable。 */
export async function initDrawtoolsConfigs(token: string): Promise<DrawtoolsInit> {
  const resp = await apiFetch('/admin/config/drawtools/init', { method: 'POST', token });
  const receipt = '缺的绘图配置已经补上，可回执';
  const { ok, created } = await receiptOf(receipt, resp, (body) => shaped(receipt, body, DRAWTOOLS_INIT));
  if (!ok) throw new Error('服务端没有补建绘图配置，请重试');
  return { ok, created };
}

export function getDrawtoolsSettingsRaw(token: string): Promise<RawText> {
  return readRawConfig('绘图 settings.yaml 原文', '/admin/config/drawtools/settings/raw', token);
}

export function updateDrawtoolsSettingsRaw(token: string, content: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig('绘图 settings.yaml', '/admin/config/drawtools/settings/raw', token, content, baseHash);
}

export function getDrawtoolsCharactersRaw(token: string): Promise<RawText> {
  return readRawConfig('绘图 character_tags.yaml 原文', '/admin/config/drawtools/characters/raw', token);
}

export function updateDrawtoolsCharactersRaw(token: string, content: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig('绘图 character_tags.yaml', '/admin/config/drawtools/characters/raw', token, content, baseHash);
}

/** 立即清理绘图附件的结果。后端另带删掉的前 200 个文件，页面只念合计。 */
export interface DrawtoolsCleanup {
  ok: boolean;
  deleted_count: number;
  deleted_mb: number;
  remaining_count: number;
  remaining_mb: number;
}

const DRAWTOOLS_CLEANUP = {
  ok: BOOLEAN,
  deleted_count: NUMBER,
  deleted_mb: NUMBER,
  remaining_count: NUMBER,
  remaining_mb: NUMBER,
};

/** 清理了却读不懂回执时抛 ReceiptUnreadable：不知道删了几个，不能说成删了 0 个。 */
export async function cleanupDrawtoolsAttachments(token: string): Promise<DrawtoolsCleanup> {
  const resp = await apiFetch('/admin/config/drawtools/cleanup', { method: 'POST', token });
  const receipt = '绘图附件已经清理，可回执';
  const result = await receiptOf(receipt, resp, (body) => shaped(receipt, body, DRAWTOOLS_CLEANUP));
  if (!result.ok) throw new Error('服务端没有清理绘图附件，请重试');
  const { ok, deleted_count, deleted_mb, remaining_count, remaining_mb } = result;
  return { ok, deleted_count, deleted_mb, remaining_count, remaining_mb };
}

export function getDrawtoolsPromptRaw(token: string, name: string): Promise<RawText> {
  return readRawConfig(`绘图提示词 ${name} 的原文`, `/admin/config/drawtools/prompts/${pathPart(name)}/raw`, token);
}

export function updateDrawtoolsPromptRaw(token: string, name: string, content: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig(`绘图提示词 ${name}`, `/admin/config/drawtools/prompts/${pathPart(name)}/raw`, token, content, baseHash);
}

const ADMIN_SKILL = {
  name: NON_EMPTY_TEXT,
  dir: optional(NON_EMPTY_TEXT),
  path: optional(TEXT),
  description: optional(TEXT),
  enabled: optional(BOOLEAN),
  strategy: optional(TEXT),
  keywords: optional(TEXT_LIST),
  body_preview: optional(TEXT),
  problem: optional(TEXT),
  rule_error: optional(TEXT),
};

export async function getSkills(token: string): Promise<AdminSkill[]> {
  const what = '技能列表';
  const resp = await apiFetch('/admin/config/skills', { token });
  return shapedRows(what, await jsonOf(what, resp), ADMIN_SKILL);
}

export function getSkillRaw(token: string, dir: string): Promise<RawText> {
  return readRawConfig(`技能 ${dir} 的原文`, `/admin/config/skills/${pathPart(dir)}/raw`, token);
}

export function updateSkillRaw(token: string, dir: string, content: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig(`技能 ${dir}`, `/admin/config/skills/${pathPart(dir)}/raw`, token, content, baseHash);
}

export async function createSkill(token: string, data: AdminCreatePayload): Promise<RawSaveResult> {
  const resp = await apiFetch('/admin/config/skills', {
    method: 'POST',
    token,
    json: data,
  });
  return saveResultOf(createdReceipt(`技能 ${data.id}`), resp);
}

/** 删了却读不懂回执时抛 ReceiptUnreadable。 */
export async function deleteSkill(token: string, dir: string): Promise<void> {
  const resp = await apiFetch(`/admin/config/skills/${pathPart(dir)}`, {
    method: 'DELETE',
    token,
  });
  const receipt = nameWith(`技能 ${dir}`, '已经删掉，可回执');
  const { ok } = await receiptOf(receipt, resp, (body) => shaped(receipt, body, { ok: BOOLEAN }));
  if (!ok) throw new Error('服务端没有删掉这个技能，请重试');
}

// 后端已经规整过：开关不是布尔、地址不是字符串就是读错了，报格式不对，不在这里替它纠正。
const MCP_CLIENT = {
  id: NON_EMPTY_TEXT,
  enabled: BOOLEAN,
  share_sessions: BOOLEAN,
  transport: NON_EMPTY_TEXT,
  description: optional(TEXT),
  command: optional(TEXT),
  args: optional(TEXT_LIST),
  env: optional(recordOf(TEXT)),
  url: optional(TEXT),
  headers: optional(recordOf(TEXT)),
};

// 读不懂就抛：补成空表的话，页面会说「暂无 MCP Client」。
export async function getMcpClients(token: string): Promise<McpClient[]> {
  const what = 'MCP Client 列表';
  const resp = await apiFetch('/admin/config/mcp-clients', { token });
  return shapedRows(what, await jsonOf(what, resp), MCP_CLIENT);
}

export function getMcpClientsRaw(token: string): Promise<RawText> {
  return readRawConfig('mcp.yaml 原文', '/admin/config/mcp-clients/raw', token);
}

export function updateMcpClientsRaw(token: string, yaml: string, baseHash: string | null): Promise<RawSaveResult> {
  return writeRawConfig('mcp.yaml', '/admin/config/mcp-clients/raw', token, yaml, baseHash);
}

export interface RestartInProgress {
  target: string;
  since: string;
}

export interface RestartResult {
  scheduled: boolean;
  target: string;
  reason?: string | null;
  in_progress?: RestartInProgress | null;
}

const RESTART_RESULT = {
  scheduled: BOOLEAN,
  target: TEXT,
  reason: optional(nullable(TEXT)),
  in_progress: optional(nullable(OBJECT)),
};
const RESTART_IN_PROGRESS = { target: TEXT, since: TEXT };

function restartResultOf(what: string, value: unknown): RestartResult {
  const body = shaped(what, value, RESTART_RESULT);
  const inProgress = body.in_progress ? shaped(what, body.in_progress, RESTART_IN_PROGRESS, 'in_progress') : body.in_progress;
  return { ...body, in_progress: inProgress };
}

/** 后端回绝照常抛；请求交上了却读不懂回执时抛 ReceiptUnreadable：排没排上要看运行页。 */
export async function restartEngine(
  token: string,
  // reason 进 eventlog，调用方不同就得说不同的话，否则事后查不出是从哪按的。
  reason = '用户从设置页面请求重启引擎',
): Promise<RestartResult> {
  const resp = await apiFetch('/admin/restart', {
    method: 'POST',
    token,
    json: { target: 'engine', reason },
  });
  const receipt = '重启请求已经交上，可回执';
  return receiptOf(receipt, resp, (body) => restartResultOf(receipt, body));
}

export async function enableCodeWrite(token: string): Promise<void> {
  await apiFetch('/admin/runtime/code-write', {
    method: 'POST', token,
  });
}

// ── 热载配置（平台配置文件与全局默认 config/features.yaml）与平台清单 ──

export interface ConfigRawFile extends RawText {
  /** false = 还没接管，控制台该显示「跟随 .env」而不是一份空配置。 */
  exists: boolean;
}

export interface ConfigSaveResult extends RawSaveResult {
  /** bot 没在跑时后端只能校验 YAML 语法，会在这里说明。 */
  warnings: string[];
}

export interface ConfigFileFingerprint {
  exists: boolean;
  mtime_ns: number;
  size: number;
}

/** 一个键的值从哪一层来。legacy 是只准写在全局的键还留在平台文件老位置上。 */
export type ConfigSource = 'platform' | 'global' | 'legacy' | 'env' | 'default';

/** bot 看到的全局文件。和控制台读到的不一致，说明全局那层改完还没被这个进程采纳。 */
export interface ConfigLayerFile {
  config_path: string;
  file: ConfigFileFingerprint;
  loaded: ConfigFileFingerprint;
  parse_error: string;
}

/** 生效快照。平台的由 bot 公布，全局的由 supervisor 当场算；published=false 时其余字段都不该读。 */
export interface ConfigLiveState {
  published: boolean;
  reason?: string;
  /** bot 没写心跳时间、或写的读不懂时是 null。 */
  age_sec?: number | null;
  /** 心跳超过 90s 没更新，bot 大概失联了。 */
  stale?: boolean;
  /** 跑着的那份值确实来自磁盘上的当前文件版本。 */
  applied?: boolean;
  file: ConfigFileFingerprint;
  state?: {
    /** 平台连没连上。下面三项是各平台 live 状态都有的，平台自己的事实在 runtime 里。 */
    connected?: boolean;
    /** 当前登录的平台账号，还没登录过是空串。 */
    account?: string;
    /** 等着处理的消息条数。 */
    backlog?: number;
    config_path?: string;
    /** 磁盘上这份文件的指纹。 */
    file?: ConfigFileFingerprint;
    /** 生效值出自哪一版文件；exists=false 是一版都没采纳，按空文件算。 */
    loaded?: ConfigFileFingerprint;
    parse_error?: string;
    /** 这份快照是哪一层：global 或 platform。 */
    layer?: string;
    values?: Record<string, unknown>;
    sources?: Record<string, ConfigSource>;
    /** 每个键能写在哪几层。 */
    layers?: Record<string, string[]>;
    defaults?: ConfigLayerFile;
    paths?: Record<string, string>;
    reload?: Record<string, string>;
    notes?: Record<string, string>;
    runtime?: PlatformRuntimeFacts;
    capabilities?: PlatformCapability[];
    /** 列表项规则。由 bot 公布 —— 前端重写一遍就会和后端 coercer 漂移。 */
    input_rules?: Record<string, ConfigInputRule>;
  };
}

/** 一个列表键的单项规则。kind 决定用哪个校验器，其余字段是该校验器的参数。 */
export interface ConfigInputRule {
  kind: 'id' | 'word';
  pattern?: string;
  min?: number;
  max?: number;
  trim?: boolean;
}

/** 一项管理能力。清单由 bot 公布 —— 前端写死的话，新加的能力会在界面上无声缺席。 */
export interface PlatformCapability {
  key: string;
  name: string;
  desc: string;
  default: string;
  /** true = 给到群主时目标会被夹在他自己那个群里。 */
  group_scoped: boolean;
  /** 这项能力实际能兑现的档位。摆出兑现不了的选项等于给一个假的权限边界。 */
  grants: string[];
}

/** 配置之外、只有 bot 进程知道的事。密钥只报「设没设」，永不回传值。 */
export interface PlatformRuntimeFacts {
  pid?: number;
  queue_workers_running?: number;
  forward_bridge_running?: boolean;
  forward_bridge_endpoint?: string;
  forward_bridge_token_set?: boolean;
  group_history_capacities?: number[];
  environment?: {
    supervisor_url?: string;
    workspace?: string;
    hash_secret_set?: boolean;
  };
  volatile?: {
    cached_groups?: number;
    history_gap_groups?: number;
  };
}

/** 某个平台的 bot 有没有换上当前这份全局文件。 */
export interface FeatureAdoption {
  id: string;
  display_name: string;
  published: boolean;
  stale?: boolean;
  age_sec?: number | null;
  applied: boolean;
  reason?: string;
}

/** 只认全局的键全局文件没写时，一个平台的 bot 眼下用着的兜底。 */
export interface FeatureFallback {
  platform: string;
  display_name: string;
  /** legacy 是平台配置文件老位置上的写法，env 是平台自己的环境变量。 */
  source: Extract<ConfigSource, 'legacy' | 'env'>;
  /** 老位置在哪个文件；env 兜底是空串。 */
  file: string;
  value: unknown;
}

/** 全系统只跑一份的功能实际开不开。全局没写时按 bot 眼下用着的兜底算。 */
export interface CentralSwitchState {
  enabled: boolean;
  set_globally: boolean;
}

/** 全局默认的生效快照。applied 要求文件解析得了、每个在跑的 bot 都换上了它。 */
export interface FeaturesLiveState extends ConfigLiveState {
  platforms: FeatureAdoption[];
  /** 按键名。旧后端没有这两项。 */
  fallbacks?: Record<string, FeatureFallback[]>;
  central?: Record<string, CentralSwitchState>;
}

export type ConsoleControlKind = 'bool' | 'tristate' | 'number' | 'percent' | 'text' | 'choice' | 'words' | 'ids';

/** 一个键在控制台上的样子，外加它的路径、能写在哪几层、内置默认值。字段对着 console_schema.py 的 Control.payload()。 */
export interface ConsoleFieldSchema {
  kind: ConsoleControlKind;
  label: string;
  desc?: string;
  unit?: string;
  step?: number;
  max?: number;
  /** 界面单位和配置单位的倍数：配置按字节存、界面按 MB 填时是 1048576。 */
  scale?: number;
  choices?: [string, string][];
  empty?: string;
  placeholder?: string;
  /** 三态没配时跟着这个键推导：它取 follows_when 里的值时算开。 */
  follows?: string;
  follows_when?: string[];
  /** 三态没配、也不跟任何键时算开还是关。 */
  unset?: boolean;
  path: string;
  layers: string[];
  default: unknown;
}

export type ConsoleSpacing = 'panel' | 'loose';
export type ConsoleFrame = 'ruled' | 'boxed';

/** 布局树的节点。说明文字里用反引号标代码片段。 */
export type ConsoleNode =
  | { kind: 'block'; title: string; hint?: string; frame?: ConsoleFrame; children?: ConsoleNode[] }
  | { kind: 'grid' | 'panel' | 'loose'; children: ConsoleNode[] }
  | { kind: 'fixed'; label: string; desc?: string; children?: ConsoleNode[] }
  | { kind: 'field'; key: string; sub?: boolean; spacing?: ConsoleSpacing; children?: ConsoleNode[] }
  | { kind: 'desc'; text: string; indent?: boolean }
  | { kind: 'note'; text: string }
  | { kind: 'custom'; name: string };

export interface ConsoleSectionSchema {
  id: string;
  label: string;
  icon: string;
  note: string;
  /** 归这个分区管的配置段。空表示这一块不写配置。 */
  paths: string[];
  nodes: ConsoleNode[];
}

/** 一页的控制台 schema：分区布局树，加上布局里用到的每个键的控件说明。 */
export interface ConsoleSchema {
  version: number;
  sections: ConsoleSectionSchema[];
  fields: Record<string, ConsoleFieldSchema>;
}

export interface PlatformDryRunVerdict {
  index: number;
  text: string;
  triggered: boolean;
  signal: string;
  blocked_by: string;
  reason: string;
  /** 随机插话与模型意愿判断取决于运行时，试听不给结论。 */
  undetermined: boolean;
  cooldown_remaining: number;
  /** 以下是在场判断带出来的状态，旧服务端不返回；没开在场判断的群里多为空串。 */
  at_sec?: number;
  source?: string;
  presence?: '' | 'present' | 'absent';
  /** 安静等待里这条的去向：在等、被后来的盖过、到点由它问模型、被显式触发接走。 */
  quiet?: '' | 'waiting' | 'merged' | 'due' | 'cancelled';
  quiet_due_at?: number;
  quiet_merged?: number;
  gate?: '' | 'pending' | 'passed' | 'missed';
  gate_probability?: number;
  /** 主动回复还剩几次；null 是不限或不适用。 */
  density_left?: number | null;
  present_until?: number | null;
}

/** 试听用的在场判断配置。active 为假时在场、安静等待和主动回复密度都不参与判定。 */
export interface PlatformDryRunPresence {
  active: boolean;
  window_sec: number;
  absent_judge_probability: number;
  quiet_wait_sec: number;
  quiet_max_wait_sec: number;
  density_limit: number;
  density_window_sec: number;
}

export interface PlatformDryRunResult {
  enabled_signals: string[];
  cooldown_applied: boolean;
  presence?: PlatformDryRunPresence;
  results: PlatformDryRunVerdict[];
}

export interface PlatformDryRunMessage {
  text: string;
  group_id?: number;
  user_id?: number;
  at_me?: boolean;
  reply_to_bot?: boolean;
  at_sec?: number;
}

/** 平台驱动能发什么、有什么限制。字段对着 platforms/descriptor.py 的 PlatformCapabilities.public_dict()。 */
export interface PlatformCapabilitiesSummary {
  max_text_length: number;
  markdown: boolean;
  max_split_parts: number;
  reply_quotas: Record<string, { limit: number; window_sec: number }>;
  mention: 'none' | 'native' | 'text';
  mention_everyone: boolean;
  quote: boolean;
  reactions: string[];
  images: {
    formats: string[];
    max_bytes: number;
    animated: boolean;
    inline_with_text: boolean;
    upload_first: boolean;
  } | null;
  native_emoji: boolean;
  merged_forward: boolean;
  proactive: boolean;
  proactive_daily_limit: number;
  typing: string[];
  progress_edit: boolean;
  recall: boolean;
  edit: boolean;
  links: boolean;
}

/** 平台注册表公布的一份平台描述。字段对着 platforms/descriptor.py 的 public_dict()。 */
export interface PlatformSummary {
  id: string;
  display_name: string;
  short_name: string;
  id_shape: string;
  channels: { name: string; kind: string; hashed: boolean }[];
  has_config: boolean;
  hooks: string[];
  /** 旧版后端没有这一项。 */
  capabilities?: PlatformCapabilitiesSummary;
  /** 平台进程的入口脚本，提示人去服务器上找哪个进程。旧版后端没有。 */
  entrypoints?: string[];
  /** 适配进程要连上的那一端。旧版后端没有。 */
  connection_peer?: string;
  /** 连不上时让人先查什么。旧版后端没有。 */
  connection_hint?: string;
}

export interface PlatformCatalog {
  /** 认得的全部平台：认会话键前缀要用全表，迁移过来的历史会话可能出自别的平台。 */
  platforms: PlatformSummary[];
  /** 本实例在跑的那个平台；旧版后端不给，是 null。 */
  current: string | null;
}

const PLATFORM_SUMMARY = {
  id: NON_EMPTY_TEXT,
  display_name: TEXT,
  short_name: TEXT,
  id_shape: TEXT,
  has_config: BOOLEAN,
  hooks: TEXT_LIST,
  capabilities: optional(OBJECT),
  entrypoints: optional(TEXT_LIST),
  connection_peer: optional(TEXT),
  connection_hint: optional(TEXT),
};
const PLATFORM_CHANNEL = { name: NON_EMPTY_TEXT, kind: TEXT, hashed: BOOLEAN };

export async function getPlatforms(token: string): Promise<PlatformCatalog> {
  const what = '平台清单';
  const resp = await apiFetch('/platforms', { token });
  const body = shaped(what, await jsonOf(what, resp), { current: optional(nullable(NON_EMPTY_TEXT)) });
  const platforms = shapedRows(what, body.platforms, PLATFORM_SUMMARY, 'platforms').map((row, index) => ({
    ...row,
    channels: shapedRows(what, row.channels, PLATFORM_CHANNEL, at(at('platforms', index), 'channels')),
  }));
  return { platforms: platforms as unknown as PlatformSummary[], current: body.current ?? null };
}

const platformConfigPath = (platformId: string, leaf: string): string =>
  `/platforms/${encodeURIComponent(platformId)}/config/${leaf}`;
const featuresConfigPath = (leaf: string): string => `/features/config/${leaf}`;

// 比 readRawConfig 多一个 exists：没有它，「还没接管」和「接管了但是空的」在界面上长得一样。
const CONFIG_RAW_FILE = { ...RAW_TEXT, exists: BOOLEAN };

async function readConfigFile(what: string, path: string, token: string): Promise<ConfigRawFile> {
  const resp = await apiFetch(path, { token });
  const { content, hash, exists } = shaped(what, await jsonOf(what, resp), CONFIG_RAW_FILE);
  return { content, hash, exists };
}

// 平台和全局设置的写回总带 warnings（没有提醒是空表），和原文编辑器的回执不同。
const CONFIG_SAVE_RECEIPT = { ok: BOOLEAN, hash: TEXT, warnings: TEXT_LIST };

/** 整份写回。后端拒绝无人消费的键（400）、文件中途被别处改过（409），错误会带着原因抛出来；回执读不懂抛 ReceiptUnreadable。 */
async function writeConfigFile(
  what: string, path: string, token: string, content: string, baseHash: string | null,
): Promise<ConfigSaveResult> {
  const resp = await apiFetch(path, {
    method: 'PUT',
    token,
    json: rawWriteFields(content, baseHash),
  });
  const receipt = savedReceipt(what);
  return receiptOf(receipt, resp, (body) => {
    const { ok, hash, warnings } = shaped(receipt, body, CONFIG_SAVE_RECEIPT);
    return { ok, hash, warnings };
  });
}

const FILE_FINGERPRINT = { exists: BOOLEAN, mtime_ns: NUMBER, size: NUMBER };
const CONFIG_SOURCE = oneOf('platform', 'global', 'legacy', 'env', 'default');
const CONFIG_INPUT_RULE = {
  kind: oneOf('id', 'word'),
  pattern: optional(TEXT),
  min: optional(NUMBER),
  max: optional(NUMBER),
  trim: optional(BOOLEAN),
};
const PLATFORM_CAPABILITY = {
  key: NON_EMPTY_TEXT,
  name: TEXT,
  desc: TEXT,
  default: TEXT,
  group_scoped: BOOLEAN,
  grants: TEXT_LIST,
};
const RUNTIME_FACTS = {
  pid: optional(NUMBER),
  queue_workers_running: optional(NUMBER),
  forward_bridge_running: optional(BOOLEAN),
  forward_bridge_endpoint: optional(TEXT),
  forward_bridge_token_set: optional(BOOLEAN),
  group_history_capacities: optional(listOf(NUMBER)),
};
const RUNTIME_ENVIRONMENT = { supervisor_url: optional(TEXT), workspace: optional(TEXT), hash_secret_set: optional(BOOLEAN) };
const RUNTIME_VOLATILE = { cached_groups: optional(NUMBER), history_gap_groups: optional(NUMBER) };
const LIVE_SNAPSHOT = {
  connected: optional(BOOLEAN),
  account: optional(TEXT),
  backlog: optional(NUMBER),
  config_path: optional(TEXT),
  parse_error: optional(TEXT),
  layer: optional(TEXT),
  values: optional(OBJECT),
  sources: optional(recordOf(CONFIG_SOURCE)),
  layers: optional(recordOf(TEXT_LIST)),
  paths: optional(recordOf(TEXT)),
  reload: optional(recordOf(TEXT)),
  notes: optional(recordOf(TEXT)),
};
const CONFIG_LIVE = {
  published: BOOLEAN,
  reason: optional(TEXT),
  age_sec: optional(nullable(NUMBER)),
  stale: optional(BOOLEAN),
  applied: optional(BOOLEAN),
};

function fingerprintOf(what: string, value: unknown, path: string): ConfigFileFingerprint {
  const { exists, mtime_ns, size } = shaped(what, value, FILE_FINGERPRINT, path);
  return { exists, mtime_ns, size };
}

function layerFileOf(what: string, value: unknown, path: string): ConfigLayerFile {
  const { config_path, parse_error, file, loaded } = shaped(what, value, { config_path: TEXT, parse_error: TEXT }, path);
  return {
    config_path,
    parse_error,
    file: fingerprintOf(what, file, at(path, 'file')),
    loaded: fingerprintOf(what, loaded, at(path, 'loaded')),
  };
}

function runtimeFactsOf(what: string, value: unknown, path: string): PlatformRuntimeFacts {
  const facts = shaped(what, value, RUNTIME_FACTS, path);
  if (facts.environment !== undefined) shaped(what, facts.environment, RUNTIME_ENVIRONMENT, at(path, 'environment'));
  if (facts.volatile !== undefined) shaped(what, facts.volatile, RUNTIME_VOLATILE, at(path, 'volatile'));
  return facts as PlatformRuntimeFacts;
}

// 来源、能写的层、生效值出自哪一版文件都拿来下结论：补成空的，「没读懂」会被说成「都跟着默认」。
function liveStateOf(what: string, value: unknown): ConfigLiveState {
  const live = shaped(what, value, CONFIG_LIVE);
  const file = fingerprintOf(what, live.file, 'file');
  if (live.state === undefined) return { ...live, file };
  const snapshot = shaped(what, live.state, LIVE_SNAPSHOT, 'state');
  const read = <T>(key: string, parse: (raw: unknown, path: string) => T): Record<string, T> => (
    snapshot[key] === undefined ? {} : { [key]: parse(snapshot[key], at('state', key)) }
  );
  const state = {
    ...snapshot,
    ...read('file', (raw, path) => fingerprintOf(what, raw, path)),
    ...read('loaded', (raw, path) => fingerprintOf(what, raw, path)),
    ...read('defaults', (raw, path) => layerFileOf(what, raw, path)),
    ...read('runtime', (raw, path) => runtimeFactsOf(what, raw, path)),
    ...read('capabilities', (raw, path) => shapedRows(what, raw, PLATFORM_CAPABILITY, path)),
    ...read('input_rules', (raw, path) => shapedRecord(what, raw, CONFIG_INPUT_RULE, path)),
  };
  return { ...live, file, state: state as NonNullable<ConfigLiveState['state']> };
}

const FEATURE_ADOPTION = {
  id: NON_EMPTY_TEXT,
  display_name: TEXT,
  published: BOOLEAN,
  stale: optional(BOOLEAN),
  age_sec: optional(nullable(NUMBER)),
  applied: BOOLEAN,
  reason: optional(TEXT),
};
// value 是平台那边写着的原样，什么类型都可能。
const FEATURE_FALLBACK = { platform: NON_EMPTY_TEXT, display_name: TEXT, source: oneOf('legacy', 'env'), file: TEXT, value: PRESENT };
const CENTRAL_SWITCH = { enabled: BOOLEAN, set_globally: BOOLEAN };

function featuresStateOf(what: string, value: unknown): FeaturesLiveState {
  const body = shaped(what, value, {});
  const fallbacks = body.fallbacks === undefined ? {} : {
    fallbacks: Object.fromEntries(
      Object.entries(shaped(what, body.fallbacks, {}, 'fallbacks'))
        .map(([name, rows]) => [name, shapedRows(what, rows, FEATURE_FALLBACK, at('fallbacks', name))]),
    ),
  };
  const central = body.central === undefined ? {} : { central: shapedRecord(what, body.central, CENTRAL_SWITCH, 'central') };
  return {
    ...liveStateOf(what, body),
    platforms: shapedRows(what, body.platforms, FEATURE_ADOPTION, 'platforms'),
    ...fallbacks,
    ...central,
  };
}

const CONSOLE_NODE_KINDS = ['block', 'grid', 'panel', 'loose', 'fixed', 'field', 'desc', 'note', 'custom'] as const;
type ConsoleNodeKind = (typeof CONSOLE_NODE_KINDS)[number];
const CONSOLE_NODE_FIELDS: Readonly<Record<ConsoleNodeKind, Shape>> = {
  block: { title: TEXT, hint: optional(TEXT), frame: optional(oneOf('ruled', 'boxed')) },
  grid: {},
  panel: {},
  loose: {},
  fixed: { label: TEXT, desc: optional(TEXT) },
  field: { key: NON_EMPTY_TEXT, sub: optional(BOOLEAN), spacing: optional(oneOf('panel', 'loose')) },
  desc: { text: TEXT, indent: optional(BOOLEAN) },
  note: { text: TEXT },
  custom: { name: NON_EMPTY_TEXT },
};
// 排布容器总发 children（可以是空表），别的节点没有子节点时不发这一项。
const CONSOLE_CONTAINERS: ReadonlySet<ConsoleNodeKind> = new Set(['grid', 'panel', 'loose']);

function consoleNodesOf(what: string, value: unknown, path: string): ConsoleNode[] {
  return shapedRows(what, value, { kind: oneOf(...CONSOLE_NODE_KINDS) }, path).map((row, index) => {
    const nodePath = at(path, index);
    const node = shaped(what, row, CONSOLE_NODE_FIELDS[row.kind], nodePath);
    if (row.children === undefined && !CONSOLE_CONTAINERS.has(row.kind)) return node as unknown as ConsoleNode;
    return { ...node, children: consoleNodesOf(what, row.children, at(nodePath, 'children')) } as unknown as ConsoleNode;
  });
}

const CHOICE_PAIR: Kind<[string, string]> = {
  ok: (value): value is [string, string] => Array.isArray(value) && value.length === 2 && value.every(TEXT.ok),
  mismatch: '不是「值、名字」两个字符串',
};
const CONSOLE_FIELD = {
  kind: oneOf('bool', 'tristate', 'number', 'percent', 'text', 'choice', 'words', 'ids'),
  label: TEXT,
  desc: optional(TEXT),
  unit: optional(TEXT),
  step: optional(NUMBER),
  max: optional(NUMBER),
  scale: optional(NUMBER),
  choices: optional(listOf(CHOICE_PAIR)),
  empty: optional(TEXT),
  placeholder: optional(TEXT),
  follows: optional(TEXT),
  follows_when: optional(TEXT_LIST),
  unset: optional(BOOLEAN),
  path: TEXT,
  layers: TEXT_LIST,
  default: PRESENT,
};
const CONSOLE_SECTION = { id: NON_EMPTY_TEXT, label: TEXT, icon: TEXT, note: TEXT, paths: TEXT_LIST };

// 认不出的节点照猜着画，页面会少一块或摆错控件，看不出是读错了。
function consoleSchemaOf(what: string, value: unknown): ConsoleSchema {
  const body = shaped(what, value, { version: NUMBER });
  return {
    version: body.version,
    sections: shapedRows(what, body.sections, CONSOLE_SECTION, 'sections').map((section, index) => ({
      ...section,
      nodes: consoleNodesOf(what, section.nodes, at(at('sections', index), 'nodes')),
    })),
    fields: shapedRecord(what, body.fields, CONSOLE_FIELD, 'fields'),
  };
}

export function getPlatformRaw(token: string, platformId: string): Promise<ConfigRawFile> {
  return readConfigFile(`平台 ${platformId} 的配置原文`, platformConfigPath(platformId, 'raw'), token);
}

export function updatePlatformRaw(
  token: string, platformId: string, content: string, baseHash: string | null,
): Promise<ConfigSaveResult> {
  return writeConfigFile(`平台 ${platformId} 的配置`, platformConfigPath(platformId, 'raw'), token, content, baseHash);
}

export async function getPlatformState(token: string, platformId: string): Promise<ConfigLiveState> {
  const what = nameWith(`平台 ${platformId}`, '的生效状态');
  const resp = await apiFetch(platformConfigPath(platformId, 'state'), { token });
  return liveStateOf(what, await jsonOf(what, resp));
}

/** 平台页的分区布局与控件说明。取自平台代码里的声明，bot 没起来也拿得到。 */
export async function getPlatformSchema(token: string, platformId: string): Promise<ConsoleSchema> {
  const what = nameWith(`平台 ${platformId}`, '的控制台布局');
  const resp = await apiFetch(platformConfigPath(platformId, 'schema'), { token });
  return consoleSchemaOf(what, await jsonOf(what, resp));
}

export function getFeaturesRaw(token: string): Promise<ConfigRawFile> {
  return readConfigFile('features.yaml 原文', featuresConfigPath('raw'), token);
}

export function updateFeaturesRaw(token: string, content: string, baseHash: string | null): Promise<ConfigSaveResult> {
  return writeConfigFile('features.yaml', featuresConfigPath('raw'), token, content, baseHash);
}

export async function getFeaturesState(token: string): Promise<FeaturesLiveState> {
  const what = '全局设置的生效状态';
  const resp = await apiFetch(featuresConfigPath('state'), { token });
  return featuresStateOf(what, await jsonOf(what, resp));
}

export async function getFeaturesSchema(token: string): Promise<ConsoleSchema> {
  const what = '全局设置的控制台布局';
  const resp = await apiFetch(featuresConfigPath('schema'), { token });
  return consoleSchemaOf(what, await jsonOf(what, resp));
}

const DRY_RUN_PRESENCE = {
  active: BOOLEAN,
  window_sec: NUMBER,
  absent_judge_probability: NUMBER,
  quiet_wait_sec: NUMBER,
  quiet_max_wait_sec: NUMBER,
  density_limit: NUMBER,
  density_window_sec: NUMBER,
};
const DRY_RUN_VERDICT = {
  index: NUMBER,
  text: TEXT,
  triggered: BOOLEAN,
  signal: TEXT,
  blocked_by: TEXT,
  reason: TEXT,
  undetermined: BOOLEAN,
  cooldown_remaining: NUMBER,
  at_sec: optional(NUMBER),
  source: optional(TEXT),
  presence: optional(oneOf('', 'present', 'absent')),
  quiet: optional(oneOf('', 'waiting', 'merged', 'due', 'cancelled')),
  quiet_due_at: optional(NUMBER),
  quiet_merged: optional(NUMBER),
  gate: optional(oneOf('', 'pending', 'passed', 'missed')),
  gate_probability: optional(NUMBER),
  density_left: optional(nullable(NUMBER)),
  present_until: optional(nullable(NUMBER)),
};

/** 试听。config 省略时用 bot 公布的生效值，省得调用方自己拼一份不一样的。 */
export async function dryRunPlatformTrigger(
  token: string,
  platformId: string,
  messages: PlatformDryRunMessage[],
  options: { config?: Record<string, unknown>; cooldown?: boolean } = {},
): Promise<PlatformDryRunResult> {
  const body: Record<string, unknown> = { messages };
  if (options.config) body.config = options.config;
  if (options.cooldown === false) body.cooldown = false;
  const resp = await apiFetch(`/platforms/${encodeURIComponent(platformId)}/trigger/dry-run`, {
    method: 'POST',
    token,
    json: body,
  });
  // 缺一项就会把「会不会接话」「被谁拦下」念错。
  const what = '试听结果';
  const result = shaped(what, await jsonOf(what, resp), { enabled_signals: TEXT_LIST, cooldown_applied: BOOLEAN });
  return {
    enabled_signals: result.enabled_signals,
    cooldown_applied: result.cooldown_applied,
    ...(result.presence === undefined ? {} : { presence: shaped(what, result.presence, DRY_RUN_PRESENCE, 'presence') }),
    results: shapedRows(what, result.results, DRY_RUN_VERDICT, 'results'),
  };
}

/** 重读了却读不懂回执时抛 ReceiptUnreadable。 */
export async function reloadConfig(token: string): Promise<{ ok: boolean }> {
  const resp = await apiFetch('/config/reload', {
    method: 'POST',
    token,
  });
  const receipt = '配置已经重新读取，可回执';
  const result = await receiptOf(receipt, resp, (body) => shaped(receipt, body, { ok: BOOLEAN, config: OBJECT }));
  if (!result.ok) throw new Error('服务端没有重新读取配置，请重试');
  return result;
}

/** 重载工具的回执：几个重载序号，engine 比对它们决定要不要重新扫一遍工具。 */
export interface ToolsReload {
  ok: boolean;
  run_id: string;
  seq: number;
  mcp_config_seq: number;
  mcp_reload_seq: number;
}

const TOOLS_RELOAD = { ok: BOOLEAN, run_id: TEXT, seq: NUMBER, mcp_config_seq: NUMBER, mcp_reload_seq: NUMBER };

/** 交上了却读不懂回执时抛 ReceiptUnreadable。 */
export async function reloadTools(token: string): Promise<ToolsReload> {
  const resp = await apiFetch('/tools/reload', {
    method: 'POST',
    token,
  });
  const receipt = '工具重载已经交上，可回执';
  const result = await receiptOf(receipt, resp, (body) => shaped(receipt, body, TOOLS_RELOAD));
  if (!result.ok) throw new Error('服务端没有重载工具，请重试');
  const { ok, run_id, seq, mcp_config_seq, mcp_reload_seq } = result;
  return { ok, run_id, seq, mcp_config_seq, mcp_reload_seq };
}

export async function getAllToolNames(token: string): Promise<string[]> {
  const what = '工具名清单';
  const resp = await apiFetch('/admin/config/all-tool-names', { token });
  return shapedValue(what, await jsonOf(what, resp), TEXT_LIST);
}

export interface EffectiveTool {
  name: string;
  registered: boolean;
  external: boolean;
  /** 内置工具走不走服务端策略要看源码，后端不猜，返回 null。 */
  guarded: boolean | null;
  /** 非空 = 这个工具会在构建工具表时被摘掉，理由就是这段文字。 */
  gated: string;
  /** 非空 = 引擎不看勾选自己会加上这个工具，什么时候给就是这段文字。 */
  automatic: string;
}

export interface EffectiveTools {
  node_id: string;
  mode: string;
  /** 配了却不存在的名字。白名单里是「这条从没生效过」，禁用名单里是「以为禁了其实没禁」。 */
  dead_names: string[];
  /** 这个节点实际能调的那些，不是它列在 yaml 里的那些。 */
  tools: EffectiveTool[];
}

const EFFECTIVE_TOOL = {
  name: NON_EMPTY_TEXT,
  registered: BOOLEAN,
  external: BOOLEAN,
  guarded: nullable(BOOLEAN),
  gated: TEXT,
  automatic: TEXT,
};

// 读不懂就抛：补成空表的话，授权页一个「不可用」都不标，看着就像勾了的都能用。
export async function getEffectiveTools(token: string, nodeId: string): Promise<EffectiveTools> {
  const what = nameWith(`节点 ${nodeId}`, '实际能用的工具');
  const resp = await apiFetch(`/admin/config/nodes/${encodeURIComponent(nodeId)}/effective-tools`, {
    token,
  });
  const body = shaped(what, await jsonOf(what, resp), { node_id: TEXT, mode: TEXT, dead_names: TEXT_LIST });
  return {
    node_id: body.node_id,
    mode: body.mode,
    dead_names: body.dead_names,
    tools: shapedRows(what, body.tools, EFFECTIVE_TOOL, 'tools'),
  };
}

// ── Model config ──

// 没有读接口：/config/openai/secret 返回明文 api_key，那是 engine 取密钥的通道。
// 界面要 model / base_url / api_key_present 就走 activeProviderConfig。

/** 存上了却读不懂回执时抛 ReceiptUnreadable。 */
export async function updateModelConfig(
  token: string,
  params: { model?: string; base_url?: string; api_key?: string },
): Promise<AppConfigPublic> {
  const resp = await apiFetch('/config/openai', {
    method: 'POST',
    token,
    json: params,
  });
  const receipt = savedReceipt('模型配置');
  return receiptOf(receipt, resp, (body) => appConfigOf(receipt, body));
}

/** 会话自己的渠道覆盖：后端原样存、原样给的一份 JSON 对象，字段跟着渠道走。 */
export type SessionProviderOverride = Record<string, unknown>;

const providerOverridePath = (sessionId: string): string => `/sessions/${encodeURIComponent(sessionId)}/provider_override`;

export async function getSessionProviderOverride(sessionId: string, token: string): Promise<SessionProviderOverride> {
  const what = '会话的渠道覆盖';
  const resp = await apiFetch(providerOverridePath(sessionId), { token });
  return shaped(what, await jsonOf(what, resp), {});
}

/** 存上了却读不懂回执时抛 ReceiptUnreadable。 */
export async function updateSessionProviderOverride(
  sessionId: string,
  token: string,
  params: SessionProviderOverride,
): Promise<SessionProviderOverride> {
  // 整份替换：编辑器没摆出来的字段也得原样带回来。
  const resp = await apiFetch(providerOverridePath(sessionId), {
    method: 'PUT',
    token,
    json: params,
  });
  const receipt = savedReceipt('会话的渠道覆盖');
  return receiptOf(receipt, resp, (body) => shaped(receipt, body, {}));
}

/** 清掉了却读不懂回执时抛 ReceiptUnreadable。 */
export async function clearSessionProviderOverride(sessionId: string, token: string): Promise<SessionProviderOverride> {
  const resp = await apiFetch(providerOverridePath(sessionId), {
    method: 'DELETE',
    token,
  });
  const receipt = '会话的渠道覆盖已经清掉，可回执';
  return receiptOf(receipt, resp, (body) => shaped(receipt, body, {}));
}

// ── Approvals ──

export type ApprovalDecision = 'allow' | 'deny';

// 定案后的审批一定带着状态：拿它和这次的决定比，才知道生效的是不是这一次。
const DECIDED_APPROVAL = { ...ADMIN_APPROVAL, status: NON_EMPTY_TEXT };

/**
 * 提交审批决定。返回的是服务端当前那条审批：status 和本次决定对不上，说明它早已被别处处理过。
 * 交上了却读不懂回执时抛 ReceiptUnreadable：决定已经到了服务端，生没生效要看它现在的状态。
 */
export async function decideApproval(
  token: string | undefined,
  approvalId: string,
  decision: ApprovalDecision,
  comment = '',
): Promise<AdminApproval> {
  const resp = await apiFetch(`/approvals/${encodeURIComponent(approvalId)}`, {
    method: 'POST',
    token,
    json: { decision, comment: comment || `${decision} via web` },
  });
  const receipt = '审批决定已经交上，可回执';
  return receiptOf(receipt, resp, (body) => shaped(receipt, body, DECIDED_APPROVAL) as AdminApproval);
}

// ── Cancel ──

export interface CancelResult {
  ok: boolean;
  /** 这次取消掉的任务数。 */
  cancelled_count?: number;
}

const CANCEL_RESULT = { ok: BOOLEAN, cancelled_count: optional(NUMBER) };

/** 取消会话里正在跑和排队的任务。后端回绝照常抛；取消了却读不懂回执时抛 ReceiptUnreadable。 */
export async function cancelActiveTasks(sessionId: string): Promise<CancelResult> {
  const resp = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/cancel_active_tasks`, { method: 'POST' });
  const receipt = '取消请求已经交上，可回执';
  const result = await receiptOf(receipt, resp, (body) => shaped(receipt, body, CANCEL_RESULT));
  if (!result.ok) throw new Error('服务端没有取消任务，请重试');
  return result;
}

// ── Reset conversation ──

export interface ConversationReset {
  ok: boolean;
  old_session_id: string;
  conversation_key: string;
  /** 跟着清掉的子会话和分支数。 */
  cleared_children: number;
}

const CONVERSATION_RESET = { ok: BOOLEAN, old_session_id: TEXT, conversation_key: TEXT, cleared_children: NUMBER };

async function conversationResetOf(receipt: string, resp: Response): Promise<ConversationReset> {
  const result = await receiptOf(receipt, resp, (body) => shaped(receipt, body, CONVERSATION_RESET));
  if (!result.ok) throw new Error('服务端没有重置会话，请重试');
  return result;
}

/** 重置了却读不懂回执时抛 ReceiptUnreadable。 */
export async function resetConversation(conversationKey: string): Promise<ConversationReset> {
  const resp = await apiFetch('/conversations/reset', {
    method: 'POST',
    json: { conversation_key: conversationKey },
  });
  return conversationResetOf('会话已经重置，可回执', resp);
}

// ── Active node ──

export interface ActiveNode {
  node_id: string;
  is_override: boolean;
  default_node_id: string;
}

const ACTIVE_NODE = { node_id: TEXT, is_override: BOOLEAN, default_node_id: TEXT };

export async function getActiveNode(sessionId: string): Promise<ActiveNode> {
  const what = '会话的当前节点';
  const resp = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/active_node`);
  return shaped(what, await jsonOf(what, resp), ACTIVE_NODE);
}

export interface NodeSwitch {
  ok: boolean;
  /** 切换后接手这个会话的节点：清掉覆盖时是默认入口。 */
  target_node_id: string;
  is_override: boolean;
  default_node_id: string;
}

const NODE_SWITCH = { ok: BOOLEAN, target_node_id: TEXT, is_override: BOOLEAN, default_node_id: TEXT };

/** targetNodeId 给空串是清掉覆盖、回到默认入口。后端回绝照常抛；切好了却读不懂回执时抛 ReceiptUnreadable。 */
export const switchNode = commandAction('node.switch', async (sessionId: string, targetNodeId: string): Promise<NodeSwitch> => {
  const resp = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/switch_node`, {
    method: 'POST',
    json: { target_node_id: targetNodeId },
  });
  const receipt = '节点已经切换，可回执';
  const result = await receiptOf(receipt, resp, (body) => shaped(receipt, body, NODE_SWITCH));
  if (!result.ok) throw new Error('服务端没有切换节点，请重试');
  return result;
});

// ── App config ──

export interface AppConfigPublic {
  version?: number;
  provider: string;
  openai: { model: string; base_url: string; api_key_present?: boolean };
  /** 后端配的默认入口（runtime 的 shell.entry_node_id）：消息没带入口节点时交给它。 */
  default_entry_node_id: string;
}

// 默认入口要拿去替人挑入口：不是字符串就算没读成，当成没配会挑错。
const APP_CONFIG = { version: optional(NUMBER), provider: TEXT, openai: OBJECT, default_entry_node_id: TEXT };
const OPENAI_PUBLIC = { model: TEXT, base_url: TEXT, api_key_present: optional(BOOLEAN) };

function appConfigOf(what: string, value: unknown): AppConfigPublic {
  const body = shaped(what, value, APP_CONFIG);
  return { ...body, openai: shaped(what, body.openai, OPENAI_PUBLIC, 'openai') };
}

export async function getConfig(): Promise<AppConfigPublic> {
  const what = '全局配置';
  const resp = await apiFetch('/config');
  return appConfigOf(what, await jsonOf(what, resp));
}

/** getConfig 的旧名：插件拿到的是整个 api 模块，删了会断插件。 */
export function getAppConfig(): Promise<AppConfigPublic> {
  return getConfig();
}

// ── List sessions ──

export interface SessionListItem {
  session_id: string;
  conversation_key: string;
  channel: string;
  created_at: string;
  updated_at: string;
}

const SESSION_ITEM = {
  session_id: NON_EMPTY_TEXT, conversation_key: TEXT, channel: TEXT, created_at: TEXT, updated_at: TEXT,
};

/** 一页会话，按最近更新排在前。返回条数少于 limit 就是到底了。 */
export async function listSessions(channel = 'web', limit = 50, offset = 0): Promise<SessionListItem[]> {
  const what = '会话列表';
  const query = new URLSearchParams({ channel, limit: String(limit) });
  if (offset > 0) query.set('offset', String(offset));
  const resp = await apiFetch(`/sessions?${query}`);
  return shapedRows(what, await jsonOf(what, resp), SESSION_ITEM);
}

// ── Delete session ──

/** 后端回绝照常抛；删掉了却读不懂回执时抛 ReceiptUnreadable。 */
export async function deleteSession(sessionId: string): Promise<{ ok: boolean }> {
  const resp = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
  const receipt = '会话已经删掉，可回执';
  const result = await receiptOf(receipt, resp, (body) => shaped(receipt, body, { ok: BOOLEAN }));
  if (!result.ok) throw new Error('服务端未确认删除会话，请重试');
  return result;
}

// ── Session messages (legacy, flat text) ──

/** 消息正文：纯文本，或者带图片、文件的多模态块。 */
export type MessageContent = string | Array<Record<string, unknown>>;

const MESSAGE_CONTENT: Kind<MessageContent> = {
  ok: (value): value is MessageContent => typeof value === 'string' || (Array.isArray(value) && value.every(isJsonObject)),
  mismatch: '不是字符串，也不是对象数组',
};

export async function getSessionMessages(sessionId: string, limit = 200): Promise<{ role: string; content: MessageContent }[]> {
  const what = '会话消息';
  const resp = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}`);
  return shapedRows(what, await jsonOf(what, resp), { role: TEXT, content: MESSAGE_CONTENT });
}

// ── Structured history (ConversationStore) ──

export interface StructuredThinkingBlock {
  text: string;
  started_at?: string;
  ended_at?: string;
  // 新旧后端的时间戳有下划线和驼峰两种写法。
  startedAt?: string;
  endedAt?: string;
}

export interface StructuredMessage {
  id: string;
  /** 在完整历史里的位置，从 0 起。往前翻页时拿最早一条的 cursor 当 before。老后端不给。 */
  cursor?: number;
  role: string;
  content: MessageContent;
  message_type?: string;
  created_at?: string;
  source_node_id?: string;
  source_task_id?: string;
  llm_request_id?: string;
  child_session_id?: string;
  child_task_id?: string;
  child_node_id?: string;
  caller_node_id?: string;
  summary?: string;
  dispatch_task_id?: string;
  dispatch_node_id?: string;
  thinking?: string | StructuredThinkingBlock;
  thinking_text?: string;
  thinking_blocks?: StructuredThinkingBlock[];
  // Clonoth format: {id, name, arguments(object)}
  tool_calls?: Array<{ id?: string; name: string; arguments?: Record<string, unknown> }>;
  tool_call_id?: string;
  tool_name?: string;
  name?: string;
  reasoning_started_at?: string;
  reasoning_ended_at?: string;
}

export interface ChildSessionInfo {
  session_id: string;
  parent_session_id?: string;
  route_parent_session_id?: string;
  node_id?: string;
  context_key?: string;
  context_mode?: string;
  status?: string;
  task_id?: string;
  started_at?: string;
  updated_at?: string;
  completed_at?: string;
}

export interface SessionHistoryQuery {
  limit?: number;
  /** 只要完整历史里位置在它之前的那些，取其中最后 limit 条。 */
  before?: number;
  /** 只要这个任务产生的消息。 */
  taskId?: string;
  signal?: AbortSignal;
}

// 其余字段拼卡片时逐个看类型；这几项不对，翻页接不上或者整页渲染不出来。
const HISTORY_ROW = {
  id: TEXT,
  cursor: optional(NUMBER),
  role: TEXT,
  content: MESSAGE_CONTENT,
  tool_calls: optional(listOf(OBJECT)),
  thinking_blocks: optional(listOf(OBJECT)),
};

export async function getSessionHistory(sessionId: string, query: SessionHistoryQuery = {}): Promise<StructuredMessage[]> {
  const what = '历史消息';
  const params = new URLSearchParams({ limit: String(query.limit ?? 200) });
  if (query.before !== undefined) params.set('before', String(query.before));
  if (query.taskId) params.set('task_id', query.taskId);
  const resp = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/history?${params}`, { signal: query.signal });
  return shapedRows(what, await jsonOf(what, resp), HISTORY_ROW) as unknown as StructuredMessage[];
}

const CHILD_SESSION = {
  session_id: NON_EMPTY_TEXT,
  parent_session_id: optional(TEXT),
  route_parent_session_id: optional(TEXT),
  node_id: optional(TEXT),
  context_key: optional(TEXT),
  context_mode: optional(TEXT),
  status: optional(TEXT),
  task_id: optional(TEXT),
  started_at: optional(TEXT),
  updated_at: optional(TEXT),
  completed_at: optional(TEXT),
};

export async function getSessionChildren(sessionId: string): Promise<ChildSessionInfo[]> {
  const what = '子会话列表';
  const resp = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/children`);
  return shapedRows(what, await jsonOf(what, resp), CHILD_SESSION);
}

/** 会话里（含入口分支）还在排队或在跑的任务。挂起等子任务的不算。 */
export interface SessionRunningTask {
  task_id: string;
  node_id: string;
  status: 'running' | 'pending';
  created_at: string;
  caller_task_id: string;
  is_user_entry: boolean;
  source_inbound_seq?: number | null;
  branch_session_id: string;
  parent_session_id: string;
}

const RUNNING_TASK = {
  task_id: NON_EMPTY_TEXT,
  node_id: TEXT,
  status: TEXT,
  created_at: TEXT,
  caller_task_id: TEXT,
  is_user_entry: BOOLEAN,
  source_inbound_seq: optional(nullable(NUMBER)),
  branch_session_id: TEXT,
  parent_session_id: TEXT,
};

// 补成空表就是「什么都没在跑」：会话的生成中标记会被错清掉。
// 挂起等子 agent 的也要：不带的话，正等子 agent 的会话重连后被标成空闲。
export async function getSessionRunningTasks(sessionId: string): Promise<SessionRunningTask[]> {
  const what = '运行中的任务列表';
  const resp = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/running_tasks?include_suspended=true`);
  const body = shaped(what, await jsonOf(what, resp), {});
  return shapedRows(what, body.tasks, RUNNING_TASK, 'tasks') as SessionRunningTask[];
}

// ── 运行诊断 ──

export interface WorkerHealth {
  alive: boolean;
  pid: number | null;
  uptime_sec: number;
  failures: number;
  respawns: number;
  given_up: boolean;
  last_exit_code: number | null;
  last_log: string;
  retry_in_sec: number;
  generation: string;
}

export interface RuntimeStatus {
  /** false = 这个部署的 engine 不由 supervisor 拉起，worker 那栏是「测不到」而不是「都挂了」。 */
  supervised: boolean;
  workers: Record<string, WorkerHealth>;
  tasks: { queued: number; running: number };
  started_at: string;
  uptime_sec: number;
}

export interface LogFileInfo {
  name: string;
  size: number;
  modified: number;
}

const WORKER_HEALTH = {
  alive: BOOLEAN,
  pid: nullable(NUMBER),
  uptime_sec: NUMBER,
  failures: NUMBER,
  respawns: NUMBER,
  given_up: BOOLEAN,
  last_exit_code: nullable(NUMBER),
  last_log: TEXT,
  retry_in_sec: NUMBER,
  generation: TEXT,
};
const RUNTIME_STATUS = { supervised: BOOLEAN, workers: OBJECT, tasks: OBJECT, started_at: TEXT, uptime_sec: NUMBER };
const TASK_COUNTS = { queued: NUMBER, running: NUMBER };

// worker 补成空表就是「一个都没起来」，任务数补成 0 就是「什么都没在跑」。
export async function getRuntimeStatus(token: string): Promise<RuntimeStatus> {
  const what = '运行状态';
  const resp = await apiFetch('/admin/runtime/status', { token });
  const body = shaped(what, await jsonOf(what, resp), RUNTIME_STATUS);
  return {
    ...body,
    workers: shapedRecord(what, body.workers, WORKER_HEALTH, 'workers'),
    tasks: shaped(what, body.tasks, TASK_COUNTS, 'tasks'),
  };
}

const LOG_FILE = { name: NON_EMPTY_TEXT, size: NUMBER, modified: NUMBER };

// 补成空表就是「还没有日志文件」。
export async function listLogFiles(token: string): Promise<LogFileInfo[]> {
  const what = '日志列表';
  const resp = await apiFetch('/admin/runtime/logs', { token });
  const body = shaped(what, await jsonOf(what, resp), {});
  return shapedRows(what, body.files, LOG_FILE, 'files');
}

export async function readLogTail(
  token: string, name: string, lines = 500,
): Promise<{ text: string; truncated: boolean }> {
  const what = nameWith(`日志 ${name}`, '的内容');
  const resp = await apiFetch(
    `/admin/runtime/logs/${encodeURIComponent(name)}?lines=${lines}`,
    { token },
  );
  return shaped(what, await jsonOf(what, resp), { text: TEXT, truncated: BOOLEAN });
}

export async function retryEngine(token: string): Promise<void> {
  await apiFetch('/admin/runtime/engine/retry', { method: 'POST', token });
}

// ── Legacy compat exports ──

export const sendInbound = postInbound;

export interface PluginSelectors {
  nodes: string[];
  channels: string[];
  providers: string[];
  purposes: string[];
}

export interface PluginParamSchema {
  key?: string;
  name?: string;
  label?: string;
  kind?: string;
  type?: string;
  default?: unknown;
  description?: string;
  desc?: string;
  choices?: unknown[];
  options?: unknown[];
  minimum?: number;
  maximum?: number;
  required?: boolean;
}

export interface AdminPlugin {
  kind?: 'module' | 'package';
  source_editable?: boolean;
  files?: Array<{ path: string; size: number; hash: string }>;
  frontend?: { entry: { path: string; hash: string } | null; styles: Array<{ path: string; hash: string }> };
  id: string;
  name: string;
  version: string;
  description: string;
  author: string;
  source: 'custom' | 'bundled';
  filename: string;
  source_hash: string;
  desired_hash: string;
  enabled: boolean;
  config: Record<string, unknown>;
  selectors: PluginSelectors;
  params_schema: PluginParamSchema[];
  metadata: Record<string, unknown>;
  error: string | null;
}

export interface PluginRuntimeProcess {
  pid: number;
  process: 'engine' | 'supervisor';
  alive: boolean;
  generation: string;
  updated_at: string;
  plugins: Array<{
    id: string;
    desired_hash: string;
    status: 'loaded' | 'disabled' | 'error' | 'retiring' | 'stopped' | 'not_applicable';
    active_requests?: number;
    error?: string;
    errors?: Array<{ phase: string; message: string; at: string }>;
  }>;
}

export interface PluginsResponse {
  plugins: AdminPlugin[];
  generation: string;
  shared_group: string | null;
  runtime: PluginRuntimeProcess[];
}

// 插件页拿来渲染、拿来当下一次保存基准的几项；其余字段照原样留着。
const PLUGIN_ROW = {
  id: NON_EMPTY_TEXT,
  name: TEXT,
  version: TEXT,
  description: TEXT,
  author: TEXT,
  filename: TEXT,
  source: oneOf('custom', 'bundled'),
  kind: optional(oneOf('module', 'package')),
  source_hash: TEXT,
  desired_hash: NON_EMPTY_TEXT,
  enabled: BOOLEAN,
  config: OBJECT,
  selectors: OBJECT,
  metadata: OBJECT,
  error: nullable(TEXT),
};
const PLUGIN_SELECTORS = { nodes: TEXT_LIST, channels: TEXT_LIST, providers: TEXT_LIST, purposes: TEXT_LIST };
// 进程状态只核页面拿来定位、比较的几项：错误详情是各进程报上来的原样，核严了，一个进程报得怪整页就读不出来。
const PLUGIN_PROCESS = { pid: NUMBER, process: TEXT, alive: BOOLEAN };
const PROCESS_PLUGIN = { id: NON_EMPTY_TEXT, desired_hash: TEXT, status: TEXT };
const PACKAGE_FILE = { path: TEXT, size: NUMBER };

function pluginRow(what: string, value: unknown, path = ''): AdminPlugin {
  const row = shaped(what, value, PLUGIN_ROW, path);
  shaped(what, row.selectors, PLUGIN_SELECTORS, at(path, 'selectors'));
  shapedRows(what, row.params_schema, {}, at(path, 'params_schema'));
  if (row.files !== undefined) shapedRows(what, row.files, PACKAGE_FILE, at(path, 'files'));
  return row as unknown as AdminPlugin;
}

function pluginProcesses(what: string, value: unknown, path: string): PluginRuntimeProcess[] {
  return shapedRows(what, value, PLUGIN_PROCESS, path).map((process, index) => {
    shapedRows(what, process.plugins, PROCESS_PLUGIN, at(at(path, index), 'plugins'));
    return process as unknown as PluginRuntimeProcess;
  });
}

const importedReceipt = (what: string): string => nameWith(what, '已经导入，可回执');

// 读不懂就抛：补成空表，页面会说「尚无插件」；缺了指纹，保存按钮一声不响地点不了，或者保存被拒。
export async function getPlugins(token: string, signal?: AbortSignal): Promise<PluginsResponse> {
  const what = '插件列表';
  const resp = await apiFetch('/admin/plugins', { token, signal });
  const body = shaped(what, await jsonOf(what, resp), { generation: TEXT, shared_group: nullable(TEXT) });
  return {
    ...body,
    plugins: shapedRows(what, body.plugins, {}, 'plugins').map((row, index) => pluginRow(what, row, at('plugins', index))),
    runtime: pluginProcesses(what, body.runtime, 'runtime'),
  } as unknown as PluginsResponse;
}

export async function getPluginSource(token: string, id: string): Promise<{ content: string; hash: string }> {
  const what = `插件 ${id} 的源码`;
  const resp = await apiFetch(`/admin/plugins/${encodeURIComponent(id)}/source`, { token });
  const { content, hash } = shaped(what, await jsonOf(what, resp), RAW_TEXT);
  return { content, hash };
}

export async function installPlugin(token: string, filename: string, content: string): Promise<AdminPlugin> {
  const resp = await apiFetch('/admin/plugins', {
    method: 'POST', token,
    json: { filename, content },
  });
  const receipt = importedReceipt(`插件 ${filename}`);
  return receiptOf(receipt, resp, (body) => pluginRow(receipt, body));
}

export async function installPluginPackage(token: string, file: File, baseHash?: string, current: () => boolean = () => true): Promise<AdminPlugin> {
  if (file.size > 32 * 1024 * 1024) throw new Error('插件包不能超过 32 MiB');
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (bytes.length > 32 * 1024 * 1024) throw new Error('插件包不能超过 32 MiB');
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  if (!current()) throw new Error('登录状态已变化，请重新导入插件包');
  const resp = await apiFetch('/admin/plugins/packages', {
    method: 'POST', token,
    json: { filename: file.name, content_base64: btoa(binary), ...(baseHash ? { base_hash: baseHash } : {}) },
  });
  const receipt = importedReceipt(`插件包 ${file.name}`);
  return receiptOf(receipt, resp, (body) => pluginRow(receipt, body));
}

export async function updatePluginSource(token: string, id: string, content: string, baseHash: string): Promise<AdminPlugin> {
  const resp = await apiFetch(`/admin/plugins/${encodeURIComponent(id)}/source`, {
    method: 'PUT', token,
    json: { content, base_hash: baseHash },
  });
  const receipt = savedReceipt(`插件 ${id} 的源码`);
  return receiptOf(receipt, resp, (body) => pluginRow(receipt, body));
}

export async function updatePluginConfig(
  token: string, id: string,
  config: Pick<AdminPlugin, 'enabled' | 'config' | 'selectors'> & { base_hash: string },
): Promise<AdminPlugin> {
  const resp = await apiFetch(`/admin/plugins/${encodeURIComponent(id)}/config`, {
    method: 'PUT', token,
    json: config,
  });
  const receipt = savedReceipt(`插件 ${id} 的配置`);
  return receiptOf(receipt, resp, (body) => pluginRow(receipt, body));
}

export async function deletePlugin(token: string, id: string, baseHash: string): Promise<void> {
  await apiFetch(`/admin/plugins/${encodeURIComponent(id)}?base_hash=${encodeURIComponent(baseHash)}`, {
    method: 'DELETE', token,
  });
}

export async function applyPlugins(token: string): Promise<Pick<PluginsResponse, 'generation' | 'runtime'> & { message: string }> {
  const resp = await apiFetch('/admin/plugins/apply', { method: 'POST', token });
  const receipt = '插件配置已经应用，可回执';
  return receiptOf(receipt, resp, (body) => {
    const { generation, message, runtime } = shaped(receipt, body, { generation: TEXT, message: TEXT });
    return { generation, message, runtime: pluginProcesses(receipt, runtime, 'runtime') };
  });
}
