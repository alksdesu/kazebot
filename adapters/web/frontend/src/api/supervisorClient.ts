// [2026-05-16] Real Supervisor API client — zero mock.
import type { NodeDef, SupervisorEvent } from '../types';

/** 本实例挂载的路径前缀。多开时几个号共用一个域名，各自挂在 /i/<号>/ 下。 */
export const MOUNT = (() => {
  // lastIndexOf：前缀本身含 web 时（/web/1/web/），从前往后找会截出空串，
  // 于是这个实例的请求全部打到根实例上去。
  const at = window.location.pathname.lastIndexOf('/web/');
  return at > 0 ? window.location.pathname.slice(0, at) : '';
})();

const API = `${MOUNT}/v1`;

// ── Helper ──

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const resp = await fetch(`${API}${path}`, init);
  if (!resp.ok) {
    let detail = '';
    try { const j = await resp.json(); detail = j.detail || ''; } catch { /* ignore */ }
    throw new Error(`${resp.status}${detail ? ` ${detail}` : ''}`);
  }
  return resp;
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

/**
 * 附件的可访问地址。path 是工作区相对路径，例如 data/attachments/xxx/yyy.png。
 *
 * token 走 query 而不是请求头：img 标签带不了 Authorization（后端两种都认）。
 */
export function attachmentHref(path: string, token: string | null): string {
  const safe = path.split('/').map(encodeURIComponent).join('/');
  return `${API}/files/${safe}${token ? `?token=${encodeURIComponent(token)}` : ''}`;
}

// ── Attachment upload ──

export interface UploadedAttachment {
  path: string;
  name: string;
  size: number;
  mime_type: string;
  type: 'image' | 'file';
}

export async function uploadAttachment(
  file: File,
  conversationKey: string,
  token: string,
): Promise<UploadedAttachment> {
  const form = new FormData();
  form.append('file', file);
  const resp = await fetch(
    `${API}/attachments/upload?conversation_key=${encodeURIComponent(conversationKey)}`,
    // 不手动设 Content-Type：浏览器要自己填 multipart boundary。
    { method: 'POST', body: form, headers: authHeaders(token) },
  );
  if (!resp.ok) {
    let detail = '';
    try { const j = await resp.json(); detail = j.detail || ''; } catch { /* ignore */ }
    throw new Error(`Upload failed: ${resp.status}${detail ? ` ${detail}` : ''}`);
  }
  return resp.json();
}

// ── Inbound (send message) ──

export async function postInbound(params: {
  conversation_key: string;
  text: string;
  attachments?: any[];
  use_context?: boolean;
  entry_node_id?: string;
}): Promise<{ session_id: string; inbound_seq: number; accepted: boolean }> {
  const resp = await apiFetch('/inbound', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      channel: 'web',
      conversation_key: params.conversation_key,
      text: params.text,
      attachments: params.attachments ?? [],
      use_context: params.use_context ?? true,
      entry_node_id: params.entry_node_id,
    }),
  });
  return resp.json();
}

// ── WebSocket events ──

// [2026-06-03] Global Supervisor WebSocket replaces per-session polling.
// Why: a browser tab can display several web sessions while more than one task is
// running, so closing or replacing the socket per active session loses events from
// the other sessions. How: keep one long-lived /v1/ws connection and let the store
// route each SupervisorEvent by event.session_id. Purpose: realtime delivery no
// longer depends on the fragile in-memory polling buffer or on the selected chat.
let _globalWs: WebSocket | null = null;
let _globalWsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
let _globalWsReconnectDelay = 1000;

export function connectGlobalWS(
  lastSeq: number,
  onEvent: (event: SupervisorEvent) => void,
  onOpen?: () => void,
  onDisconnect?: () => void,
): void {
  // [2026-06-03] Why: loadStartup and sendMessage can both request realtime setup.
  // How: do not open a second socket while one is CONNECTING or OPEN. Purpose:
  // global event delivery stays single-copy while the connection remains long-lived.
  const readyState = _globalWs?.readyState;
  if (readyState === 0 || readyState === 1) {
    return;
  }

  disconnectGlobalWS();

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${window.location.host}${MOUNT}/v1/ws`;
  const ws = new WebSocket(wsUrl);
  _globalWs = ws;

  ws.onopen = () => {
    // [2026-06-03] Why: /v1/ws uses a global sequence cursor, unlike the old
    // per-session endpoint. How: send the latest known EventLog seq across all
    // sessions once the socket opens. Purpose: reconnect catch-up covers every
    // session without session event polling.
    _globalWsReconnectDelay = 1000;
    ws.send(JSON.stringify({ last_seq: Math.max(0, lastSeq || 0) }));
    onOpen?.();
  };

  ws.onmessage = (msgEvent) => {
    try {
      const data = JSON.parse(msgEvent.data);
      if (data.type === 'ping') return;
      onEvent(data as SupervisorEvent);
    } catch {
      // [2026-06-03] Why: the global stream is append-only and should tolerate
      // malformed frames. How: ignore the bad frame only. Purpose: one malformed
      // payload cannot stop future session events.
    }
  };

  ws.onclose = () => {
    if (_globalWs === ws) _globalWs = null;
    onDisconnect?.();
    // [2026-06-03] Reconnect is handled by the store's onDisconnect callback
    // (startGlobalWebSocket). The transport layer only tracks backoff delay.
    _globalWsReconnectDelay = Math.min(_globalWsReconnectDelay * 2, 30000);
  };

  ws.onerror = () => {
    // [2026-06-03] Why: network errors should share the same cleanup and reconnect
    // callback as normal disconnects. How: close the socket and let onclose run.
    // Purpose: the store has one recovery path for long-lived global realtime.
    ws.close();
  };
}

export function disconnectGlobalWS(): void {
  // [2026-06-03] Why: resetState and tests still need an explicit cleanup point.
  // How: clear the pending reconnect marker and close only the global socket.
  // Purpose: ordinary task completion never calls this, but full teardown remains safe.
  if (_globalWsReconnectTimer) { clearTimeout(_globalWsReconnectTimer); _globalWsReconnectTimer = null; }
  if (_globalWs) {
    _globalWs.onclose = null;
    _globalWs.close();
    _globalWs = null;
  }
}

export function connectSessionWS(
  sessionId: string,
  lastSeq: number,
  onEvent: (event: SupervisorEvent) => void,
  onOpen?: () => void,
  onDisconnect?: () => void,
): void {
  // [2026-06-03] Compatibility wrapper. Why: the legacy chatStore still imports the
  // old per-session helper. How: route it through the global WebSocket and filter by
  // session id at the client boundary. Purpose: avoid keeping two WebSocket designs
  // while chatStore has moved to all-session realtime delivery.
  connectGlobalWS(
    lastSeq,
    (event) => { if (event.session_id === sessionId) onEvent(event); },
    onOpen,
    onDisconnect,
  );
}

export function disconnectSessionWS(): void {
  // [2026-06-03] Compatibility wrapper. Why: existing legacy call sites still call
  // disconnectSessionWS during teardown. How: delegate to the global cleanup helper.
  // Purpose: reset paths remain functional without reintroducing per-session sockets.
  disconnectGlobalWS();
}

// ── Health ──

export interface HealthState {
  status: string;
  run_id?: string;
  workspace_root?: string;
  started_at?: string;
  uptime_seconds?: number;
}

export async function checkHealth(): Promise<HealthState> {
  const resp = await apiFetch('/health');
  return resp.json();
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
}

export interface AdminState {
  sessions: number;
  approvals: Record<string, number>;
  tasks: Record<string, number>;
  pending_approvals: AdminApproval[];
  engine_runtime: Record<string, unknown>;
}

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
}

export interface AdminNode extends NodeDef {
  tool_access?: unknown;
  skills?: unknown;
  /** false = 模板或示例文件，不会被派发到。 */
  active?: boolean;
  /** yaml 里写的 id，且和文件名不一致。engine 认文件名，这个字段改了不换节点。 */
  declared_id?: string;
}

export type ToolSource = 'builtin' | 'plugin' | 'external';

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
}

export interface AdminSkill {
  name: string;
  description?: string;
  enabled?: boolean;
  strategy?: string;
  keywords?: string[];
  body_preview?: string;
  error?: string;
}

export interface McpClient {
  id: string;
  description?: string;
  enabled?: boolean;
  transport?: string;
  command?: string;
  args?: string[];
  env?: Record<string, unknown>;
  url?: string;
  headers?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface AdminCreatePayload {
  id: string;
  content: string;
}

async function readRawConfig(path: string, token: string): Promise<string> {
  // [2026-06-02] Shared raw-config reader for the expanded Settings tabs.
  // Why: nodes, tools, skills, MCP clients, schedules, policy, and runtime all expose
  // the same {content:string} shape. How: centralize bearer auth and response
  // unwrapping in one helper. Purpose: page code edits text without duplicating API
  // response handling or accidentally returning the wrapper object.
  const resp = await apiFetch(path, { headers: authHeaders(token) });
  const json = await resp.json();
  return typeof json.content === 'string' ? json.content : '';
}

async function writeRawConfig(path: string, token: string, content: string): Promise<any> {
  // [2026-06-02] Shared raw-config writer for the expanded Settings tabs.
  // Why: every raw editor saves through the same {content:string} backend model. How:
  // send JSON with the admin bearer header in one helper. Purpose: the UI can add new
  // raw-backed pages without reimplementing method, headers, and payload shape.
  const resp = await apiFetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ content }),
  });
  return resp.json();
}

const pathPart = (value: string): string => encodeURIComponent(value);

export async function getAdminState(token: string): Promise<AdminState> {
  // [2026-06-01] Fetch the protected Supervisor snapshot for the chat dashboard.
  // Why: the new right rail needs session, approval, task, and engine worker counts.
  // How: call /v1/admin/state with the existing bearer token helper. Purpose: keep
  // dashboard polling inside the API client instead of scattering endpoint strings.
  const resp = await apiFetch('/admin/state', { headers: authHeaders(token) });
  return resp.json();
}

export async function fetchActiveTasks(token = ''): Promise<ActiveTask[]> {
  // [AutoC 2026-06-04] Why: the System dashboard task count now opens a detail
  // modal. How: call the new protected summary endpoint with the same bearer-token
  // helper used by other admin APIs. Purpose: task monitoring stays independent of
  // chatStore and does not duplicate request construction in UI components.
  const init = token ? { headers: authHeaders(token) } : undefined;
  const resp = await fetch(`${API}/admin/tasks/active`, init);
  if (!resp.ok) throw new Error(`Failed to fetch active tasks: ${resp.status}`);
  return resp.json();
}

export async function cancelTask(adminToken: string, taskId: string): Promise<void> {
  // [AutoC 2026-06-04] Why: ActiveTasksModal cancels one task at a time, while the
  // existing cancelActiveTasks helper targets an entire session. How: call the
  // public single-task endpoint and still include admin context headers for future
  // backend hardening. Purpose: row-level cancel buttons remain precise and safe.
  const headers = adminToken
    ? { ...authHeaders(adminToken), 'X-Admin-Token': adminToken }
    : {};
  const resp = await fetch(`${API}/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: 'POST',
    headers,
  });
  if (!resp.ok) throw new Error(`Cancel failed: ${resp.status}`);
}

// ── Admin auth ──

export async function checkAdminAuth(token: string): Promise<boolean> {
  try {
    const resp = await fetch(`${API}/admin/auth/check`, { headers: authHeaders(token) });
    return resp.ok;
  } catch {
    return false;
  }
}

// ── Nodes ──

export async function getNodes(token: string): Promise<AdminNode[]> {
  const resp = await apiFetch('/admin/config/nodes', { headers: authHeaders(token) });
  return resp.json();
}

export function getConfigRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/config/raw', token);
}

export function updateConfigRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/config/raw', token, yaml);
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

export async function getProviders(token: string): Promise<ProvidersResponse> {
  const resp = await apiFetch('/config/providers', { headers: authHeaders(token) });
  return resp.json();
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
export async function getProviderProfiles(token: string): Promise<ProviderProfiles> {
  const resp = await apiFetch('/config/provider-options', { headers: authHeaders(token) });
  const body = await resp.json();
  return {
    options: (body?.options || {}) as ProviderOptionsCatalog,
    wireFormats: (body?.wire_formats || {}) as Record<string, string>,
    hostProfiles: (body?.host_profiles || {}) as Record<string, string[]>,
    defaultVision: (body?.default_vision || {}) as Record<string, boolean>,
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
  const resp = await apiFetch(`/config/providers/${encodeURIComponent(name)}/models`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  const body = await resp.json();
  return Array.isArray(body?.models) ? body.models.map(String) : [];
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
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteProvider(token: string, name: string): Promise<ProvidersResponse> {
  const resp = await apiFetch(`/config/providers/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
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

export async function getSystemModels(token: string): Promise<SystemModelsResponse> {
  const resp = await apiFetch('/config/system-models', { headers: authHeaders(token) });
  return resp.json();
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
}

export async function getPolicy(token: string): Promise<PolicyDoc> {
  const resp = await apiFetch('/config/policy', { headers: authHeaders(token) });
  const data = await resp.json();
  // 拿不到 policy 字段就给空文档：调用方按对象用，返回 undefined 会让渲染直接炸。
  return (data && data.policy) || {};
}

/** 整份替换。部分更新没法表达「删掉一条规则」，后端也按整份校验。 */
export async function updatePolicy(token: string, policy: PolicyDoc): Promise<PolicyDoc> {
  const resp = await apiFetch('/config/policy', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ policy }),
  });
  const data = await resp.json();
  return (data && data.policy) || {};
}

// ── bot 登录账号（经 supervisor 转发 NapCat WebUI）──

/** 此前登录过、可以免扫码切回去的号。 */
export interface QqQuickLoginTarget {
  uin: string;
  nick?: string;
  avatar?: string;
  /** 切过一次被 QQ 拒了。NapCat 自报的 isQuickLogin 在这种情况下仍会给 true。 */
  available?: boolean;
  dead_reason?: string;
}

export interface QqAccount {
  configured: boolean;
  /** NapCat 有没有应答。容器重启期间为 false，这不是错误。 */
  reachable?: boolean;
  reason?: string;
  uin?: string;
  nick?: string;
  online?: boolean;
  is_login?: boolean;
  login_error?: string;
  /** 等扫码时 NapCat 会把二维码一并带出来。 */
  qrcode?: string;
  quick_login?: QqQuickLoginTarget[];
}

/** 一个后端实例 = 一个 QQ 号。多开时它们共用域名，各自挂在 path 下。 */
export interface ConsoleInstance {
  uin: string;
  label: string;
  /** 挂载前缀，根实例为空串。 */
  path: string;
  current: boolean;
  /** 序号，决定端口。老清单没有这个字段时后端给 -1。 */
  idx?: number;
}

/**
 * 另一个实例的控制台地址。
 *
 * 换的是后端不是页面，所以只能整页跳；带上 query 才会停在同一页而不是弹回第一页。
 */
export function instanceConsoleHref(path: string): string {
  return `${path}/web/${window.location.search}`;
}

/**
 * 这个号归不归别的实例管。归了就不能从本实例登过去 —— 同一个 QQ 双登，
 * 两边会互相把对方踢下线，数据也会分成两份。
 *
 * 清单为空（单实例，或读不到）时一律返回 null：宁可不拦，也不能把所有号都锁死。
 */
export function ownedByAnotherInstance(
  uin: string,
  instances: ConsoleInstance[],
): ConsoleInstance | null {
  const owner = instances.find((row) => row.uin === uin);
  return owner && !owner.current ? owner : null;
}

export async function getInstances(token: string): Promise<ConsoleInstance[]> {
  const resp = await apiFetch('/instances', { headers: authHeaders(token) });
  const data = await resp.json();
  return Array.isArray(data?.instances) ? data.instances : [];
}

/** 新实例落点。序号决定端口，前缀决定它挂在域名的哪一层。 */
export interface InstancePlan {
  uin: string;
  label: string;
  idx: number;
  prefix: string;
  ports: Record<string, number>;
  workspace: string;
}

/** root 侧建号的实时进度。finished 之前一直轮询。 */
export interface InstanceProgress {
  uin: string;
  lines: string[];
  finished: boolean;
  ok: boolean;
  detail: string;
}

export async function createInstance(
  token: string,
  uin: string,
  label: string,
): Promise<InstancePlan> {
  const resp = await apiFetch('/instances', {
    method: 'POST',
    headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
    body: JSON.stringify({ uin, label }),
  });
  return resp.json();
}

export async function getInstanceProgress(
  token: string,
  uin: string,
): Promise<InstanceProgress> {
  const resp = await apiFetch(`/instances/${encodeURIComponent(uin)}/progress`, {
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function deleteInstance(token: string, uin: string): Promise<void> {
  await apiFetch(`/instances/${encodeURIComponent(uin)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
}

export async function getQqAccount(token: string): Promise<QqAccount> {
  const resp = await apiFetch('/qq/account', { headers: authHeaders(token) });
  const data = await resp.json();
  return data || { configured: false };
}

export async function qqQuickLogin(token: string, uin: string): Promise<void> {
  await apiFetch('/qq/account/quick-login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ uin }),
  });
}

/** 清掉自动登录并重启容器：NapCat 在已登录状态下拒发二维码，也没有登出接口。 */
export async function qqEnterLoginMode(token: string): Promise<void> {
  await apiFetch('/qq/account/relogin', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: '{}',
  });
}

/** 把当前在线的号钉成自动登录，否则下次容器重启会停在等扫码。 */
export async function qqPinAccount(token: string): Promise<void> {
  await apiFetch('/qq/account/pin', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: '{}',
  });
}

export interface QqQrcode {
  qrcode: string;
  /** false 表示 NapCat 还没起来，等下一轮即可，不必当成失败。 */
  reachable: boolean;
  reason?: string;
}

export async function getQqLoginQrcode(token: string): Promise<QqQrcode> {
  const resp = await apiFetch('/qq/account/qrcode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: '{}',
  });
  const data = await resp.json();
  return {
    qrcode: String((data && data.qrcode) || ''),
    reachable: (data && data.reachable) !== false,
    reason: String((data && data.reason) || ''),
  };
}

/** 字段留空串表示删掉这一项（回到跟随主渠道）；不传表示不动。 */
export async function updateSystemModel(
  token: string,
  slot: string,
  data: { base_url?: string; api_key?: string; model?: string; provider?: string },
): Promise<SystemModelsResponse> {
  const resp = await apiFetch(`/config/system-models/${encodeURIComponent(slot)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

/** 配了多个生图渠道时默认用哪个。空串 = 交给模型按用途判断。 */
export async function updateImageDefaultChannel(
  token: string,
  defaultChannel: string,
): Promise<SystemModelsResponse> {
  const resp = await apiFetch('/config/image-default-channel', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ default_channel: defaultChannel }),
  });
  return resp.json();
}

export async function setActiveProvider(token: string, provider: string): Promise<ProvidersResponse> {
  const resp = await apiFetch('/config/active-provider', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ provider }),
  });
  return resp.json();
}

export async function updateFallbacks(
  token: string,
  fallbacks: FallbackEntryUpdate[],
): Promise<ProvidersResponse> {
  const resp = await apiFetch('/config/fallbacks', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ fallbacks }),
  });
  return resp.json();
}

export async function updateNodeFallbacks(
  token: string,
  nodeId: string,
  fallbacks: FallbackEntryUpdate[],
): Promise<ProvidersResponse> {
  const resp = await apiFetch(`/config/node-fallbacks/${encodeURIComponent(nodeId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ fallbacks }),
  });
  return resp.json();
}

// 删掉专属链 = 这个节点回退到全局链。提交空数组是「禁用」，两者不同。
export async function clearNodeFallbacks(
  token: string,
  nodeId: string,
): Promise<ProvidersResponse> {
  const resp = await apiFetch(`/config/node-fallbacks/${encodeURIComponent(nodeId)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

export function getRuntimeRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/runtime/raw', token);
}

export function updateRuntimeRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/runtime/raw', token, yaml);
}

export function getPolicyRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/policy/raw', token);
}

export function updatePolicyRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/policy/raw', token, yaml);
}

// ── Memory & conversation context ──

export interface MemoryOwner {
  kind: 'group' | 'private' | 'agent' | 'subject' | 'unknown';
  label: string;
  alias?: string;
  real_id?: string;
  conversation_key?: string;
}

export interface MemoryNamespace {
  key: string;
  namespace: string;
  kind: 'conversation' | 'subject' | 'orphan';
  subject: string;
  entry_count: number;
  books: string[];
  updated_at: string;
  owner: MemoryOwner;
}

export interface MemoryEntry {
  id: string;
  book: string;
  content: string;
  keywords?: string[];
  constant?: boolean;
  enabled?: boolean;
  priority?: number;
  scan_depth?: number;
  subject?: string;
  source?: string;
  created_at?: string;
  updated_at?: string;
}

export async function getMemoryOverview(token: string): Promise<MemoryNamespace[]> {
  const resp = await apiFetch('/admin/config/memory/overview', { headers: authHeaders(token) });
  return (await resp.json()).namespaces || [];
}

export async function getMemoryEntries(token: string, namespace: string): Promise<MemoryEntry[]> {
  const resp = await apiFetch(
    `/admin/config/memory/${encodeURIComponent(namespace)}/entries`, { headers: authHeaders(token) },
  );
  return (await resp.json()).entries || [];
}

export async function saveMemoryEntry(
  token: string, namespace: string, entry: Partial<MemoryEntry>,
): Promise<MemoryEntry> {
  const resp = await apiFetch(`/admin/config/memory/${encodeURIComponent(namespace)}/entries`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(entry),
  });
  return (await resp.json()).entry;
}

export async function deleteMemoryEntry(
  token: string, namespace: string, book: string, entryId: string,
): Promise<void> {
  await apiFetch(
    `/admin/config/memory/${encodeURIComponent(namespace)}/entries/${encodeURIComponent(book)}/${encodeURIComponent(entryId)}`,
    { method: 'DELETE', headers: authHeaders(token) },
  );
}

export async function clearMemoryNamespace(token: string, namespace: string): Promise<number> {
  const resp = await apiFetch(`/admin/config/memory/${encodeURIComponent(namespace)}`, {
    method: 'DELETE', headers: authHeaders(token),
  });
  return (await resp.json()).removed || 0;
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

export async function getConversations(token: string): Promise<ConversationRow[]> {
  const resp = await apiFetch('/admin/conversations', { headers: authHeaders(token) });
  return (await resp.json()).conversations || [];
}

export async function getConversationMessages(
  token: string, sessionId: string, limit = 200,
): Promise<{ total: number; messages: Array<Record<string, any>> }> {
  const resp = await apiFetch(
    `/admin/conversations/${encodeURIComponent(sessionId)}/messages?limit=${limit}`,
    { headers: authHeaders(token) },
  );
  return resp.json();
}

export async function resetConversationBySession(token: string, sessionId: string): Promise<any> {
  const resp = await apiFetch(`/admin/conversations/${encodeURIComponent(sessionId)}/reset`, {
    method: 'POST', headers: authHeaders(token),
  });
  return resp.json();
}

export interface ScopePlanRow {
  old_namespace: string;
  new_namespace: string;
  has_memory: boolean;
  blocked: boolean;
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
  } | null;
}

export async function getQqScope(token: string, target = ''): Promise<ScopeStatus> {
  const query = target ? `?target=${encodeURIComponent(target)}` : '';
  const resp = await apiFetch(`/admin/qq/scope${query}`, { headers: authHeaders(token) });
  return resp.json();
}

export async function migrateQqScope(token: string, target: string, source?: string): Promise<any> {
  const resp = await apiFetch('/admin/qq/scope/migrate', {
    method: 'POST',
    headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
    body: JSON.stringify(source === undefined ? { target } : { target, source }),
  });
  return resp.json();
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

/** 预览图地址。token 走 query 而不是请求头：img 标签带不了 Authorization。 */
export function stickerImageHref(sha256: string, token: string | null): string {
  const query = token ? `?token=${encodeURIComponent(token)}` : '';
  return `${API}/stickers/${encodeURIComponent(sha256)}/image${query}`;
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
  const resp = await apiFetch(`/stickers?${query}`, { headers: authHeaders(token) });
  return resp.json();
}

export async function updateSticker(
  token: string,
  sha256: string,
  patch: { name?: string; manual_tags?: string[]; manual_override?: boolean },
): Promise<Sticker> {
  const resp = await apiFetch(`/stickers/${encodeURIComponent(sha256)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(patch),
  });
  return resp.json();
}

export async function acceptSticker(token: string, sha256: string): Promise<Sticker> {
  const resp = await apiFetch(`/stickers/${encodeURIComponent(sha256)}/accept`, {
    method: 'POST', headers: authHeaders(token),
  });
  return resp.json();
}

export async function discardSticker(token: string, sha256: string): Promise<void> {
  await apiFetch(`/stickers/${encodeURIComponent(sha256)}/discard`, {
    method: 'POST', headers: authHeaders(token),
  });
}

export async function deleteSticker(token: string, sha256: string): Promise<void> {
  await apiFetch(`/stickers/${encodeURIComponent(sha256)}`, {
    method: 'DELETE', headers: authHeaders(token),
  });
}

export async function recaptionStickers(token: string, onlyFailed: boolean): Promise<number> {
  const resp = await apiFetch('/stickers/recaption', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ only_failed: onlyFailed }),
  });
  return (await resp.json()).queued || 0;
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
    method: 'POST', body: form, headers: authHeaders(token),
  });
  return resp.json();
}

export async function importStickers(
  token: string, path: string, accept: boolean,
): Promise<StickerImportResult> {
  const resp = await apiFetch('/stickers/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ path, accept }),
  });
  return resp.json();
}

export function getSchedulesRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/schedules/raw', token);
}

export function updateSchedulesRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/schedules/raw', token, yaml);
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
}

export async function getNodeFiles(token: string): Promise<NodeFileInfo[]> {
  const resp = await apiFetch('/admin/config/node-files', { headers: authHeaders(token) });
  return resp.json();
}

export function getNodeFileRaw(token: string, filename: string): Promise<string> {
  return readRawConfig(`/admin/config/node-files/${pathPart(filename)}/raw`, token);
}

export function updateNodeFileRaw(token: string, filename: string, content: string): Promise<any> {
  return writeRawConfig(`/admin/config/node-files/${pathPart(filename)}/raw`, token, content);
}

export async function makeNodeFileExample(token: string, filename: string): Promise<{ ok: boolean; created: boolean; path: string }> {
  const resp = await apiFetch(`/admin/config/node-files/${pathPart(filename)}/make-example`, {
    method: 'POST',
    headers: authHeaders(token),
  });
  return resp.json();
}

export function getNodeRaw(token: string, nodeId: string): Promise<string> {
  return readRawConfig(`/admin/config/nodes/${pathPart(nodeId)}/raw`, token);
}

export function updateNodeRaw(token: string, nodeId: string, yaml: string): Promise<any> {
  return writeRawConfig(`/admin/config/nodes/${pathPart(nodeId)}/raw`, token, yaml);
}

export async function createNode(token: string, data: AdminCreatePayload): Promise<any> {
  // [2026-06-02] Create raw-backed node files through the existing admin endpoint.
  // Why: the backend currently accepts an id plus complete YAML content instead of a
  // higher-level template object. How: keep the wrapper close to that contract while
  // pages may build the content from a selected template. Purpose: node creation stays
  // compatible with Supervisor without adding another server schema.
  const resp = await apiFetch('/admin/config/nodes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteNode(token: string, nodeId: string): Promise<any> {
  const resp = await apiFetch(`/admin/config/nodes/${pathPart(nodeId)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getTools(token: string): Promise<AdminTool[]> {
  const resp = await apiFetch('/admin/config/tools', { headers: authHeaders(token) });
  return resp.json();
}

export function getToolRaw(token: string, name: string): Promise<string> {
  return readRawConfig(`/admin/config/tools/${pathPart(name)}/raw`, token);
}

export function updateToolRaw(token: string, name: string, script: string): Promise<any> {
  return writeRawConfig(`/admin/config/tools/${pathPart(name)}/raw`, token, script);
}

export async function createTool(token: string, data: AdminCreatePayload): Promise<any> {
  const resp = await apiFetch('/admin/config/tools', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteTool(token: string, name: string): Promise<any> {
  const resp = await apiFetch(`/admin/config/tools/${pathPart(name)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

// ── Drawtools ──

export interface DrawtoolsRawFile {
  content: string;
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

export async function getDrawtoolsBundle(token: string): Promise<DrawtoolsBundle> {
  const resp = await apiFetch('/admin/config/drawtools', { headers: authHeaders(token) });
  return resp.json();
}

export async function initDrawtoolsConfigs(token: string): Promise<{ ok: boolean; created: string[] }> {
  const resp = await apiFetch('/admin/config/drawtools/init', { method: 'POST', headers: authHeaders(token) });
  return resp.json();
}

export function getDrawtoolsSettingsRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/drawtools/settings/raw', token);
}

export function updateDrawtoolsSettingsRaw(token: string, content: string): Promise<any> {
  return writeRawConfig('/admin/config/drawtools/settings/raw', token, content);
}

export function getDrawtoolsCharactersRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/drawtools/characters/raw', token);
}

export function updateDrawtoolsCharactersRaw(token: string, content: string): Promise<any> {
  return writeRawConfig('/admin/config/drawtools/characters/raw', token, content);
}

export async function cleanupDrawtoolsAttachments(token: string): Promise<any> {
  const resp = await apiFetch('/admin/config/drawtools/cleanup', { method: 'POST', headers: authHeaders(token) });
  return resp.json();
}

export function getDrawtoolsPromptRaw(token: string, name: string): Promise<string> {
  return readRawConfig(`/admin/config/drawtools/prompts/${pathPart(name)}/raw`, token);
}

export function updateDrawtoolsPromptRaw(token: string, name: string, content: string): Promise<any> {
  return writeRawConfig(`/admin/config/drawtools/prompts/${pathPart(name)}/raw`, token, content);
}

export async function getSkills(token: string): Promise<AdminSkill[]> {
  const resp = await apiFetch('/admin/config/skills', { headers: authHeaders(token) });
  return resp.json();
}

export function getSkillRaw(token: string, name: string): Promise<string> {
  return readRawConfig(`/admin/config/skills/${pathPart(name)}/raw`, token);
}

export function updateSkillRaw(token: string, name: string, content: string): Promise<any> {
  return writeRawConfig(`/admin/config/skills/${pathPart(name)}/raw`, token, content);
}

export async function createSkill(token: string, data: AdminCreatePayload): Promise<any> {
  const resp = await apiFetch('/admin/config/skills', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteSkill(token: string, name: string): Promise<any> {
  const resp = await apiFetch(`/admin/config/skills/${pathPart(name)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getMcpClients(token: string): Promise<McpClient[]> {
  const resp = await apiFetch('/admin/config/mcp-clients', { headers: authHeaders(token) });
  return resp.json();
}

export function getMcpClientsRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/mcp-clients/raw', token);
}

export function updateMcpClientsRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/mcp-clients/raw', token, yaml);
}

export interface RestartResult {
  scheduled: boolean;
  target: string;
}

export async function restartEngine(
  token: string,
  // reason 进 eventlog，调用方不同就得说不同的话，否则事后查不出是从哪按的。
  reason = '用户从设置页面请求重启引擎',
): Promise<RestartResult> {
  // [2026-06-02] Expose restart as an explicit engine-only wrapper for Settings.
  // Why: the UI must not guess the RestartIn payload each time. How: send the
  // backend-required target and a Chinese reason string with admin auth. Purpose: the
  // dangerous action remains behind a confirm dialog while the API contract is fixed.
  const resp = await apiFetch('/admin/restart', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ target: 'engine', reason }),
  });
  return resp.json();
}

// ================================================================
//  QQ 控制台
// ================================================================

export interface QqRawFile {
  content: string;
  /** false = 还没接管，控制台该显示「跟随 .env」而不是一份空配置。 */
  exists: boolean;
}

export interface QqSaveResult {
  ok: boolean;
  /** bot 没在跑时后端只能校验 YAML 语法，会在这里说明。 */
  warnings: string[];
}

/** bot 公布的生效快照。published=false 时其余字段都不该读。 */
export interface QqLiveState {
  published: boolean;
  reason?: string;
  age_sec?: number;
  /** 心跳超过 90s 没更新，bot 大概失联了。 */
  stale?: boolean;
  /** bot 跑着的那份值确实来自磁盘上的当前文件版本。 */
  applied?: boolean;
  file: { exists: boolean; mtime_ns: number; size: number };
  state?: {
    config_path?: string;
    parse_error?: string;
    values?: Record<string, unknown>;
    paths?: Record<string, string>;
    reload?: Record<string, string>;
    notes?: Record<string, string>;
    runtime?: QqRuntimeFacts;
    capabilities?: QqCapability[];
    /** 列表项规则。由 bot 公布 —— 前端重写一遍就会和后端 coercer 漂移。 */
    input_rules?: Record<string, QqInputRule>;
  };
}

/** 一个列表键的单项规则。kind 决定用哪个校验器，其余字段是该校验器的参数。 */
export interface QqInputRule {
  kind: 'id' | 'word';
  pattern?: string;
  min?: number;
  max?: number;
  trim?: boolean;
}

/** 一项管理能力。清单由 bot 公布 —— 前端写死的话，新加的能力会在界面上无声缺席。 */
export interface QqCapability {
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
export interface QqRuntimeFacts {
  pid?: number;
  onebot_connected?: boolean;
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
    queue_pending?: number;
    cached_groups?: number;
    history_gap_groups?: number;
  };
}

export interface QqDryRunVerdict {
  index: number;
  text: string;
  triggered: boolean;
  signal: string;
  blocked_by: string;
  reason: string;
  /** 随机插话与模型意愿判断取决于运行时，试听不给结论。 */
  undetermined: boolean;
  cooldown_remaining: number;
}

export interface QqDryRunResult {
  enabled_signals: string[];
  cooldown_applied: boolean;
  results: QqDryRunVerdict[];
}

export interface QqDryRunMessage {
  text: string;
  group_id?: number;
  user_id?: number;
  at_me?: boolean;
  reply_to_bot?: boolean;
  at_sec?: number;
}

// 不复用 readRawConfig：那个 helper 只留 content，会把 exists 一起丢掉，
// 于是「还没接管」和「接管了但是空的」在界面上长得一样。
export async function getQqRaw(token: string): Promise<QqRawFile> {
  const resp = await apiFetch('/admin/config/qq/raw', { headers: authHeaders(token) });
  const json = await resp.json();
  return {
    content: typeof json.content === 'string' ? json.content : '',
    exists: Boolean(json.exists),
  };
}

/** 整份写回。后端拒绝无人消费的键（400），错误会带着那批键名抛出来。 */
export async function updateQqRaw(token: string, content: string): Promise<QqSaveResult> {
  const resp = await apiFetch('/admin/config/qq/raw', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ content }),
  });
  const json = await resp.json();
  return { ok: Boolean(json.ok), warnings: Array.isArray(json.warnings) ? json.warnings : [] };
}

export async function getQqState(token: string): Promise<QqLiveState> {
  const resp = await apiFetch('/admin/config/qq/state', { headers: authHeaders(token) });
  return resp.json();
}

/** 试听。config 省略时用 bot 公布的生效值，省得调用方自己拼一份不一样的。 */
export async function dryRunQqTrigger(
  token: string,
  messages: QqDryRunMessage[],
  options: { config?: Record<string, unknown>; cooldown?: boolean } = {},
): Promise<QqDryRunResult> {
  const body: Record<string, unknown> = { messages };
  if (options.config) body.config = options.config;
  if (options.cooldown === false) body.cooldown = false;
  const resp = await apiFetch('/admin/config/qq/trigger/dry-run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(body),
  });
  return resp.json();
}

export async function reloadConfig(token: string): Promise<any> {
  const resp = await apiFetch('/config/reload', {
    method: 'POST',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function reloadTools(token: string): Promise<any> {
  const resp = await apiFetch('/tools/reload', {
    method: 'POST',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getAllToolNames(token: string): Promise<string[]> {
  const resp = await apiFetch('/admin/config/all-tool-names', { headers: authHeaders(token) });
  return resp.json();
}

export interface EffectiveTool {
  name: string;
  registered: boolean;
  external: boolean;
  /** 内置工具走不走服务端策略要看源码，后端不猜，返回 null。 */
  guarded: boolean | null;
  /** 非空 = 这个工具会在构建工具表时被摘掉，理由就是这段文字。 */
  gated: string;
}

export interface EffectiveTools {
  node_id: string;
  mode: string;
  /** 配了却不存在的名字。白名单里是「这条从没生效过」，禁用名单里是「以为禁了其实没禁」。 */
  dead_names: string[];
  /** 这个节点实际能调的那些，不是它列在 yaml 里的那些。 */
  tools: EffectiveTool[];
}

export async function getEffectiveTools(token: string, nodeId: string): Promise<EffectiveTools> {
  const resp = await apiFetch(`/admin/config/nodes/${encodeURIComponent(nodeId)}/effective-tools`, {
    headers: authHeaders(token),
  });
  return resp.json();
}

// ── Model config ──

// 没有读接口：/config/openai/secret 返回明文 api_key，那是 engine 取密钥的通道。
// 界面要 model / base_url / api_key_present 就走 activeProviderConfig。

export async function updateModelConfig(
  token: string,
  params: { model?: string; base_url?: string; api_key?: string },
): Promise<any> {
  const resp = await apiFetch('/config/openai', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(params),
  });
  return resp.json();
}

export type SessionProviderOverride = Record<string, unknown>;

export async function getSessionProviderOverride(sessionId: string, token: string): Promise<SessionProviderOverride> {
  // [2026-06-01] Fetch session-scoped provider overrides.
  // Why: the right panel must show the effective session model/base_url instead of
  // only the global OpenAI defaults. How: call Supervisor's admin-protected
  // provider_override endpoint with the same bearer token used by config APIs.
  // Purpose: model edits can affect only the selected session.
  const resp = await apiFetch(`/sessions/${sessionId}/provider_override`, { headers: authHeaders(token) });
  return resp.json();
}

export async function updateSessionProviderOverride(
  sessionId: string,
  token: string,
  params: SessionProviderOverride,
): Promise<SessionProviderOverride> {
  // [2026-06-01] Save session-scoped provider overrides.
  // Why: global model updates are too broad for the requested session panel. How:
  // PUT the complete override object, preserving fields the compact editor does not
  // touch. Purpose: allow per-session model, provider, api_key, and base_url edits.
  const resp = await apiFetch(`/sessions/${sessionId}/provider_override`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(params),
  });
  return resp.json();
}

export async function clearSessionProviderOverride(sessionId: string, token: string): Promise<SessionProviderOverride> {
  // [2026-06-01] Clear session-scoped provider overrides.
  // Why: the session panel needs a direct way back to node/global defaults. How:
  // call DELETE on the same provider_override resource. Purpose: avoid saving empty
  // model fields that would be hard to distinguish from inherited defaults.
  const resp = await apiFetch(`/sessions/${sessionId}/provider_override`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

// ── Approvals ──

export async function decideApproval(
  token: string,
  approvalId: string,
  decision: 'allow' | 'deny',
  comment = '',
): Promise<any> {
  const resp = await apiFetch(`/approvals/${approvalId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ decision, comment: comment || `${decision} via web` }),
  });
  return resp.json();
}

// ── Cancel ──

export async function cancelActiveTasks(sessionId: string): Promise<any> {
  const resp = await apiFetch(`/sessions/${sessionId}/cancel_active_tasks`, { method: 'POST' });
  return resp.json();
}

// ── Reset conversation ──

export async function resetConversation(conversationKey: string): Promise<any> {
  const resp = await apiFetch('/conversations/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_key: conversationKey }),
  });
  return resp.json();
}

// ── Active node ──

export async function getActiveNode(sessionId: string): Promise<{
  node_id: string;
  is_override: boolean;
  default_node_id: string;
}> {
  const resp = await apiFetch(`/sessions/${sessionId}/active_node`);
  return resp.json();
}

export async function switchNode(sessionId: string, targetNodeId: string): Promise<any> {
  const resp = await apiFetch(`/sessions/${sessionId}/switch_node`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target_node_id: targetNodeId }),
  });
  return resp.json();
}

// ── App config (public, no auth needed) ──

export interface AppConfigPublic {
  version?: number;
  provider: string;
  openai: { model: string; base_url: string; api_key_present?: boolean };
  entry_node_id?: string;
  default_entry_node_id?: string;
  shell?: { entry_node_id?: string };
}

export async function getConfig(): Promise<AppConfigPublic> {
  // [2026-06-02] Expose the public config endpoint under its route name as well as
  // getAppConfig. Why: the Client settings page needs to read the currently
  // configured entry_node_id when the backend includes it, while older callers still
  // use getAppConfig for model display. How: return the same /v1/config response with
  // optional entry-node fields in the TypeScript shape. Purpose: selection state can
  // prefer real Supervisor configuration without breaking existing config consumers.
  const resp = await apiFetch('/config');
  return resp.json();
}

export function getAppConfig(): Promise<AppConfigPublic> {
  // [2026-06-02] Keep the historical helper as an alias to getConfig.
  // Why: Header and session panels already call getAppConfig. How: delegate to the
  // route-named wrapper instead of duplicating fetch logic. Purpose: both old and new
  // settings code share one public config contract.
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

export async function listSessions(channel = 'web', limit = 50): Promise<SessionListItem[]> {
  try {
    const resp = await fetch(`${API}/sessions?channel=${channel}&limit=${limit}`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

// ── Delete session ──

export async function deleteSession(sessionId: string): Promise<{ ok: boolean }> {
  try {
    const resp = await fetch(`${API}/sessions/${sessionId}`, { method: 'DELETE' });
    if (!resp.ok) return { ok: false };
    return resp.json();
  } catch {
    return { ok: false };
  }
}

// ── Session messages (legacy, flat text) ──

export async function getSessionMessages(sessionId: string, limit = 200): Promise<{ role: string; content: string }[]> {
  try {
    const resp = await fetch(`${API}/sessions/${sessionId}/messages?limit=${limit}`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

// ── Structured history (ConversationStore) ──

export interface StructuredThinkingBlock {
  text: string;
  started_at?: string;
  ended_at?: string;
  // [AutoC 2026-06-04] Why: mixed backend versions may serialize structured
  // thinking timestamps in camelCase. How: type both naming conventions. Purpose:
  // history hydration keeps elapsed-time headers instead of falling back to length.
  startedAt?: string;
  endedAt?: string;
}

export interface StructuredMessage {
  id: string;
  role: string;
  content: string;
  message_type?: string;
  created_at?: string;
  source_node_id?: string;
  // [AutoC 2026-06-03] Why: dispatch-result history rows need the same structured
  // child navigation metadata as realtime WebSocket payloads. How: expose the
  // selected backend metadata fields returned by /history. Purpose: the UI can show
  // a child-session jump button after page refresh without parsing callback text.
  source_task_id?: string;
  // [AutoC 2026-06-04] Why: history rows may be correlated with live request cards
  // after request-scoped streaming. How: type the optional backend request id.
  // Purpose: future hydration can reconcile one persisted assistant row with one
  // request card without falling back to task-level matching.
  llm_request_id?: string;
  child_session_id?: string;
  // [AutoC 2026-06-04] Why: dispatch_result history now uses explicit child/caller
  // metadata and summary. How: expose the new fields while keeping legacy dispatch_*
  // fields typed for old rows. Purpose: TypeScript callers can render refreshed
  // callback cards from structure instead of localized content text.
  child_task_id?: string;
  child_node_id?: string;
  caller_node_id?: string;
  summary?: string;
  dispatch_task_id?: string;
  dispatch_node_id?: string;
  // [AutoC 2026-06-04] Why: historical reasoning needs the same timing metadata as
  // live ThinkingBlock cards. How: accept the new backend thinking_blocks array while
  // keeping flat string/object thinking for old rows. Purpose: refreshed history can
  // render elapsed thinking time instead of falling back to character counts.
  thinking?: string | StructuredThinkingBlock;
  thinking_text?: string;
  thinking_blocks?: StructuredThinkingBlock[];
  // Clonoth format: {id, name, arguments(object)}
  tool_calls?: Array<{ id?: string; name: string; arguments?: Record<string, unknown> }>;
  tool_call_id?: string;
  tool_name?: string;
  name?: string;
  // [thinking-time 2026-06-01] Precise reasoning timing from backend meta.
  reasoning_started_at?: string;
  reasoning_ended_at?: string;
}

export interface ChildSessionInfo {
  // [2026-06-03] Why: child session rows come from sessions.json, not chat history.
  // How: mirror the backend's snake_case response so chatStore can normalize it into
  // ChildNodeState. Purpose: refresh can restore child-node status and navigation.
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

export async function getSessionHistory(sessionId: string, limit = 200, taskId?: string): Promise<StructuredMessage[]> {
  try {
    let url = `${API}/sessions/${sessionId}/history?limit=${limit}`;
    if (taskId) url += `&task_id=${encodeURIComponent(taskId)}`;
    const resp = await fetch(url);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

export async function getSessionChildren(sessionId: string): Promise<ChildSessionInfo[]> {
  try {
    // [2026-06-03] Why: child-session metadata is stored in the supervisor registry,
    // while /history only returns messages. How: call the dedicated read-only endpoint
    // and tolerate missing older backends by returning an empty array. Purpose: the web
    // app can be deployed independently while still restoring childNodes when possible.
    const resp = await fetch(`${API}/sessions/${sessionId}/children`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
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

export async function getRuntimeStatus(token: string): Promise<RuntimeStatus> {
  const resp = await apiFetch('/admin/runtime/status', { headers: authHeaders(token) });
  return resp.json();
}

export async function listLogFiles(token: string): Promise<LogFileInfo[]> {
  const resp = await apiFetch('/admin/runtime/logs', { headers: authHeaders(token) });
  return (await resp.json()).files || [];
}

export async function readLogTail(
  token: string, name: string, lines = 500,
): Promise<{ text: string; truncated: boolean }> {
  const resp = await apiFetch(
    `/admin/runtime/logs/${encodeURIComponent(name)}?lines=${lines}`,
    { headers: authHeaders(token) },
  );
  return resp.json();
}

export async function retryEngine(token: string): Promise<void> {
  await apiFetch('/admin/runtime/engine/retry', { method: 'POST', headers: authHeaders(token) });
}

// ── Legacy compat exports ──

export const sendInbound = postInbound;
