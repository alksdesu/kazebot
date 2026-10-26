// 设置页表单和 yaml 原文之间的转换。写回只给出改动了的键，经 editYamlKeys 落到原文上，注释和其余原文照旧。
import yaml from 'js-yaml';

import { StaleKeysError } from '../../console/yamlCommit';
import {
  editYamlKeys,
  parseYamlMapping,
  readYamlPath,
  type YamlKeyEdit,
  type YamlRewrite,
} from '../../console/yamlRewrite';

export interface ScheduleFormState {
  original?: Record<string, unknown>;
  id: string;
  cron: string;
  type: 'message' | 'script';
  text: string;
  command: string;
  enabled: boolean;
  once: boolean;
  conversation_key: string;
  entry_node_id: string;
  workflow_id: string;
  timeout: string;
  silent: boolean;
}

export interface McpClientFormState {
  original?: Record<string, unknown>;
  id: string;
  description: string;
  enabled: boolean;
  /** 勾上时所有对话共用这个 Client 的会话；不勾时每个对话各用各的。 */
  share_sessions: boolean;
  transport: 'stdio' | 'sse' | 'streamable_http';
  command: string;
  argsText: string;
  envText: string;
  url: string;
  headersText: string;
}

/** 技能 frontmatter 里能在快捷表单上改的几项，全是输入框原样的文字。 */
export interface SkillQuickFields {
  enabled: boolean;
  strategy: string;
  keywords: string;
  order: string;
  priority: string;
  scan_depth: string;
}

export type NodeConfigType = 'ai' | 'tool';
// 与 engine/node.py 的 ToolAccess.mode 取值一致；后端把未知值降级成 none，
// 曾经这里写 allow/deny 导致节点被静默改成「无任何工具」。
export type ToolAccessMode = 'all' | 'allowlist' | 'none';
// engine.tool_mode 与工具权限无关，是工具调用格式；后端 _normalize_tool_mode 会把
// 下划线写法归一到连字符，未知值落回 fake-native。
export type EngineToolMode = 'native' | 'fake-native' | 'json';

export interface NodeConfigFormState {
  id: string;
  name: string;
  description: string;
  type: NodeConfigType;
  model: string;
  provider: string;
  memory_book: string;
  persistent: boolean;
  prompt: string;
  promptIsStructured?: boolean;
  delegate_targetsText: string;
}

export interface RuntimeConfigFormState {
  entry_node_id: string;
  /** 空串：本实例这层没写，跟随仓库默认。 */
  tool_mode: EngineToolMode | '';
  max_workers: string;
  compact_threshold_tokens: string;
  compact_hard_threshold_tokens: string;
  compact_keep_recent_tokens: string;
  compact_keep_recent: string;
}

const safeLoad = (raw: string): Record<string, any> => parseYamlMapping(raw) as Record<string, any>;

function str(value: any, fallback = ''): string {
  if (value === null || value === undefined) return fallback;
  return String(value);
}

// 与 clonoth_runtime.parse_bool 认同一套写法：手写的 yes、on、1 算开，no、off、0 算关，认不出的照 fallback，和引擎读出同一个结论
const PARSE_BOOL_TRUE_WORDS = new Set(['1', 'true', 'yes', 'y', 'on']);
const PARSE_BOOL_FALSE_WORDS = new Set(['0', 'false', 'no', 'n', 'off']);

export function parseBoolFlag(value: unknown, fallback = false): boolean {
  if (typeof value === 'boolean') return value;
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim().toLowerCase();
  if (PARSE_BOOL_TRUE_WORDS.has(text)) return true;
  if (PARSE_BOOL_FALSE_WORDS.has(text)) return false;
  return fallback;
}

function normalizeEngineToolMode(value: string, fallback: EngineToolMode = 'fake-native'): EngineToolMode {
  const normalized = value.trim().toLowerCase().replace(/_/g, '-');
  return normalized === 'native' || normalized === 'json' || normalized === 'fake-native' ? normalized : fallback;
}

const nested = (doc: Record<string, any>, path: readonly string[]): any => readYamlPath(doc, path);

function normalizeNodeConfigType(value: string): NodeConfigType {
  return value === 'tool' ? value : 'ai';
}

function commaTextToItems(value: string): string[] {
  return value.split(',').map((s) => s.trim()).filter(Boolean);
}

/** Convert a plain object to a textarea-friendly `key: value` text. */
function serializeLooseKV(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  return Object.entries(value as Record<string, unknown>)
    .map(([k, v]) => `${k}: ${String(v ?? '')}`)
    .join('\n');
}

/** Parse textarea `key: value` lines back into a plain object. */
function parseLooseKV(text: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf(':');
    if (idx < 0) continue;
    const key = trimmed.slice(0, idx).trim();
    const val = trimmed.slice(idx + 1).trim();
    if (key) result[key] = val;
  }
  return result;
}

/** 按项合并保存前核对：表单打开之后这一项在文件里变了，就不能拿旧表单盖掉；别的项照常合并。 */
function itemChangedSince(loaded: { original?: Record<string, unknown> }, latest: { original?: Record<string, unknown> }): boolean {
  return JSON.stringify(loaded.original ?? null) !== JSON.stringify(latest.original ?? null);
}

export function parseNodeConfig(raw: string, fallbackId = ''): NodeConfigFormState {
  const doc = safeLoad(raw);
  return {
    id: str(doc.id, fallbackId),
    name: str(doc.name),
    description: str(doc.description),
    type: normalizeNodeConfigType(str(doc.type, 'ai')),
    model: str(doc.model),
    provider: str(doc.provider),
    memory_book: str(doc.memory_book),
    persistent: parseBoolFlag(doc.persistent),
    prompt: Array.isArray(doc.prompt) ? JSON.stringify(doc.prompt, null, 2) : str(doc.prompt),
    promptIsStructured: Array.isArray(doc.prompt),
    delegate_targetsText: Array.isArray(doc.delegate_targets) ? doc.delegate_targets.map(String).join(', ') : '',
  };
}

function promptValue(form: NodeConfigFormState): unknown {
  if (!form.prompt.trim()) return undefined;
  if (!form.promptIsStructured) return form.prompt;
  let prompt: unknown;
  try {
    prompt = JSON.parse(form.prompt);
  } catch {
    throw new Error('结构化提示词必须是 JSON 数组，原文件未修改');
  }
  if (!Array.isArray(prompt)) throw new Error('结构化提示词必须是 JSON 数组，原文件未修改');
  return prompt;
}

/** 节点文件满是手写的说明注释，prompt 常是上百行的块标量：只写和原文不一样的字段，其余原文照旧。 */
export function serializeNodeConfig(raw: string, form: NodeConfigFormState): YamlRewrite {
  const current = parseNodeConfig(raw, form.id);
  const changed = (key: keyof NodeConfigFormState) => form[key] !== current[key];
  const edits: YamlKeyEdit[] = [];
  if (changed('id')) edits.push({ path: ['id'], value: form.id.trim() });
  if (changed('name')) edits.push({ path: ['name'], value: form.name.trim() });
  if (changed('description')) edits.push({ path: ['description'], value: form.description.trim() });
  if (changed('type')) edits.push({ path: ['type'], value: form.type });
  if (changed('model')) edits.push({ path: ['model'], value: form.model.trim() });
  if (changed('provider')) edits.push({ path: ['provider'], value: form.provider.trim() || undefined });
  if (changed('memory_book')) edits.push({ path: ['memory_book'], value: form.memory_book.trim() || undefined });
  if (changed('persistent')) edits.push({ path: ['persistent'], value: form.persistent });
  if (changed('prompt') || changed('promptIsStructured')) edits.push({ path: ['prompt'], value: promptValue(form) });
  if (changed('delegate_targetsText')) edits.push({ path: ['delegate_targets'], value: commaTextToItems(form.delegate_targetsText) });
  // tool_access 只能改 YAML 原文，表单不碰它：按 mode 重写会把对应不上的那半（allowlist 下的 deny）洗掉。
  return editYamlKeys(raw, edits);
}

// runtime.yaml 里这三项都在二级键下；曾经按顶层读写，于是表单永远空值，
// 保存还会注入三个无人消费的顶层键。
const RUNTIME_ENTRY_NODE_PATH = ['shell', 'entry_node_id'] as const;
const RUNTIME_TOOL_MODE_PATH = ['engine', 'tool_mode'] as const;
const RUNTIME_MAX_WORKERS_PATH = ['engine', 'max_workers'] as const;
const RUNTIME_COMPACT_THRESHOLD_PATH = ['engine', 'compact', 'threshold_tokens'] as const;
const RUNTIME_COMPACT_HARD_THRESHOLD_PATH = ['engine', 'compact', 'hard_threshold_tokens'] as const;
const RUNTIME_COMPACT_KEEP_TOKENS_PATH = ['engine', 'compact', 'keep_recent_tokens'] as const;
const RUNTIME_COMPACT_KEEP_SEGMENTS_PATH = ['engine', 'compact', 'keep_recent'] as const;

/** 数字键读成输入框文本。缺键读空串，表示本实例不覆盖、跟随仓库默认。 */
function numText(doc: Record<string, any>, path: readonly string[]): string {
  const value = nested(doc, path);
  return value === undefined || value === null ? '' : String(value);
}

/** 整数输入框回写成 yaml 的值。空串与非法值都是 undefined，也就是删掉这个键。 */
function intValue(value: string, min = 0): number | undefined {
  const parsed = Number.parseInt(value.trim(), 10);
  return Number.isFinite(parsed) && parsed >= min ? parsed : undefined;
}

const toolModeText = (value: unknown): EngineToolMode | '' => (
  value === undefined || value === null || value === '' ? '' : normalizeEngineToolMode(String(value))
);

export function parseRuntimeConfig(raw: string): RuntimeConfigFormState {
  const doc = safeLoad(raw);
  return {
    entry_node_id: str(nested(doc, RUNTIME_ENTRY_NODE_PATH)),
    tool_mode: toolModeText(nested(doc, RUNTIME_TOOL_MODE_PATH)),
    max_workers: numText(doc, RUNTIME_MAX_WORKERS_PATH),
    compact_threshold_tokens: numText(doc, RUNTIME_COMPACT_THRESHOLD_PATH),
    compact_hard_threshold_tokens: numText(doc, RUNTIME_COMPACT_HARD_THRESHOLD_PATH),
    compact_keep_recent_tokens: numText(doc, RUNTIME_COMPACT_KEEP_TOKENS_PATH),
    compact_keep_recent: numText(doc, RUNTIME_COMPACT_KEEP_SEGMENTS_PATH),
  };
}

/**
 * 写进 runtime.yaml 本实例那一层（还没有时传空串）。只写和原文不一样的字段：
 * 没动过的键留给仓库默认，原文里一百多行说明注释也照旧。
 */
export function serializeRuntimeConfig(raw: string, form: RuntimeConfigFormState): YamlRewrite {
  const current = parseRuntimeConfig(raw);
  const edits: YamlKeyEdit[] = [];
  const entryNode = form.entry_node_id.trim();
  if (entryNode !== current.entry_node_id) edits.push({ path: RUNTIME_ENTRY_NODE_PATH, value: entryNode || undefined });
  const toolMode = toolModeText(form.tool_mode);
  if (toolMode !== current.tool_mode) edits.push({ path: RUNTIME_TOOL_MODE_PATH, value: toolMode || undefined });
  const numbers: Array<[keyof RuntimeConfigFormState, readonly string[], number]> = [
    // 留空表示不覆盖默认值；写 0 会让 engine 一个 worker 都不起。
    ['max_workers', RUNTIME_MAX_WORKERS_PATH, 1],
    // 0 各有含义：软阈值 0 关掉自动压缩，硬阈值 0 取软阈值的 1.25 倍，保留量 0 改按段数算。
    ['compact_threshold_tokens', RUNTIME_COMPACT_THRESHOLD_PATH, 0],
    ['compact_hard_threshold_tokens', RUNTIME_COMPACT_HARD_THRESHOLD_PATH, 0],
    ['compact_keep_recent_tokens', RUNTIME_COMPACT_KEEP_TOKENS_PATH, 0],
    ['compact_keep_recent', RUNTIME_COMPACT_KEEP_SEGMENTS_PATH, 2],
  ];
  for (const [field, path, min] of numbers) {
    if (form[field].trim() !== current[field]) edits.push({ path, value: intValue(form[field], min) });
  }
  return editYamlKeys(raw, edits);
}

/** 文件里的定时任务原样的那几项。schedules 没写或写成空值都算没有，和 supervisor/scheduler.py 一致。 */
function scheduleList(raw: string): Record<string, any>[] {
  const list = safeLoad(raw).schedules ?? [];
  if (!Array.isArray(list) || list.some(item => !item || typeof item !== 'object' || Array.isArray(item))) throw new Error('schedules 必须是对象数组，原文件未修改');
  if (list.some(item => item.type !== undefined && !['message', 'script'].includes(item.type))) throw new Error('存在不支持的定时任务类型，请用原文编辑');
  return list;
}

function scheduleForm(item: Record<string, any>): ScheduleFormState {
  return {
    original: { ...item },
    id: str(item.id),
    cron: str(item.cron),
    type: item.type === 'script' ? 'script' : 'message',
    text: str(item.text),
    command: str(item.command),
    enabled: parseBoolFlag(item.enabled, true),
    once: parseBoolFlag(item.once),
    conversation_key: str(item.conversation_key),
    entry_node_id: str(item.entry_node_id),
    workflow_id: str(item.workflow_id),
    timeout: item.timeout === undefined ? '' : String(item.timeout),
    silent: parseBoolFlag(item.silent, true),
  };
}

export function parseSchedules(raw: string): ScheduleFormState[] {
  return scheduleList(raw).map(scheduleForm);
}

const schedulesEdit = (items: readonly Record<string, unknown>[]): YamlKeyEdit => ({ path: ['schedules'], value: items });

const scheduleTimeout = (text: string): unknown => Number(text) || text;

/** 表单改过的字段写回这一项，没动的键（含表单不认识的）原样留着；换了类型才连带清掉另一种类型的键。 */
function scheduleItem(original: Record<string, any>, form: ScheduleFormState): Record<string, unknown> {
  const before = scheduleForm(original);
  const item: Record<string, unknown> = { ...original };
  const changed = (key: keyof ScheduleFormState) => form[key] !== before[key];
  for (const key of ['cron', 'text', 'enabled', 'once'] as const) {
    if (changed(key)) item[key] = form[key];
  }
  for (const key of ['conversation_key', 'entry_node_id', 'workflow_id'] as const) {
    if (!changed(key)) continue;
    if (form[key]) item[key] = form[key]; else delete item[key];
  }
  const switched = changed('type');
  if (switched) item.type = form.type;
  if (form.type === 'script') {
    if (switched || changed('command')) item.command = form.command;
    if (switched || changed('timeout')) {
      if (form.timeout) item.timeout = scheduleTimeout(form.timeout); else delete item.timeout;
    }
    if (switched || changed('silent')) item.silent = form.silent;
  } else if (switched) {
    delete item.command; delete item.timeout; delete item.silent;
  }
  return item;
}

function newScheduleItem(form: ScheduleFormState): Record<string, unknown> {
  const item: Record<string, unknown> = {
    id: form.id, cron: form.cron, type: form.type, text: form.text, enabled: form.enabled, once: form.once,
  };
  for (const key of ['conversation_key', 'entry_node_id', 'workflow_id'] as const) {
    if (form[key]) item[key] = form[key];
  }
  if (form.type === 'script') {
    item.command = form.command;
    if (form.timeout) item.timeout = scheduleTimeout(form.timeout);
    item.silent = form.silent;
  }
  return item;
}

/** 改一项：它在打开之后被别处改过就报冲突；其余任务一字不动。 */
export function scheduleUpdateEdits(raw: string, loaded: ScheduleFormState, form: ScheduleFormState): YamlKeyEdit[] {
  const items = scheduleList(raw);
  const index = items.findIndex((item) => str(item.id) === loaded.id);
  if (index < 0) throw new Error('未在 schedules.yaml 中找到此定时任务。');
  if (itemChangedSince(loaded, { original: items[index] })) throw new StaleKeysError([`schedules.${loaded.id}`]);
  return [schedulesEdit(items.map((item, i) => (i === index ? scheduleItem(item, form) : item)))];
}

export function scheduleCreateEdits(raw: string, form: ScheduleFormState): YamlKeyEdit[] {
  const items = scheduleList(raw);
  if (items.some((item) => str(item.id) === form.id)) throw new Error('该任务 ID 已存在');
  return [schedulesEdit([...items, newScheduleItem(form)])];
}

export function scheduleRemoveEdits(raw: string, id: string): YamlKeyEdit[] {
  return [schedulesEdit(scheduleList(raw).filter((item) => str(item.id) !== id))];
}

type McpTransport = McpClientFormState['transport'];

// 与 toolbox/mcp_runtime.py 的 _normalize_transport 一致：没写或认不出的都按 stdio 起。
function normalizeMcpTransport(value: unknown): McpTransport {
  const text = String(value || 'stdio').trim().toLowerCase().replace(/-/g, '_');
  if (['streamablehttp', 'streamable_http', 'http', 'streamable'].includes(text)) return 'streamable_http';
  return text === 'sse' ? 'sse' : 'stdio';
}

const isHttpTransport = (transport: McpTransport): boolean => transport !== 'stdio';

/** 文件里的 clients 映射。没写或写成空值都算没有，和 toolbox/mcp_runtime.py 一致。 */
function mcpClientMap(raw: string): Record<string, Record<string, any>> {
  const clients = safeLoad(raw).clients ?? {};
  if (typeof clients !== 'object' || Array.isArray(clients) || Object.values(clients).some(v => !v || typeof v !== 'object' || Array.isArray(v))) throw new Error('clients 必须是对象映射，原文件未修改');
  return clients;
}

function mcpClientForm(id: string, spec: Record<string, any>): McpClientFormState {
  return {
    original: { ...spec },
    id,
    description: str(spec.description),
    enabled: parseBoolFlag(spec.enabled, true),
    share_sessions: parseBoolFlag(spec.share_sessions),
    transport: normalizeMcpTransport(spec.transport),
    command: str(spec.command),
    argsText: Array.isArray(spec.args) ? JSON.stringify(spec.args) : '[]',
    envText: serializeLooseKV(spec.env),
    url: str(spec.url),
    headersText: serializeLooseKV(spec.headers),
  };
}

export function parseMcpClients(raw: string): McpClientFormState[] {
  return Object.entries(mcpClientMap(raw)).map(([id, spec]) => mcpClientForm(id, spec));
}

function mcpArgs(text: string): string[] {
  const trimmed = text.trim();
  let args: unknown;
  if (trimmed.startsWith('[')) {
    try {
      args = JSON.parse(trimmed);
    } catch {
      throw new Error('MCP 启动参数必须是 JSON 字符串数组');
    }
  } else {
    args = text.split('\n').map((item) => item.trim()).filter(Boolean);
  }
  if (!Array.isArray(args) || args.some((item) => typeof item !== 'string')) throw new Error('MCP 启动参数必须是字符串数组');
  return args;
}

const clientPath = (id: string, ...keys: string[]): string[] => ['clients', id, ...keys];

function newMcpClientSpec(form: McpClientFormState): Record<string, unknown> {
  const spec: Record<string, unknown> = { transport: form.transport, enabled: form.enabled };
  // 只在勾上时写，和后端 upsert_client 一致：文件里没有这个键就是按对话隔离
  if (form.share_sessions) spec.share_sessions = true;
  if (form.description) spec.description = form.description;
  if (isHttpTransport(form.transport)) {
    spec.url = form.url;
    spec.headers = parseLooseKV(form.headersText);
  } else {
    spec.command = form.command;
    spec.args = mcpArgs(form.argsText);
    spec.env = parseLooseKV(form.envText);
  }
  return spec;
}

/** 只写和打开时不一样的字段；在 stdio 和 http 两类之间换了传输方式，才连带清掉另一类的键。 */
function mcpClientFieldEdits(before: McpClientFormState, after: McpClientFormState): YamlKeyEdit[] {
  const changed = (key: keyof McpClientFormState) => after[key] !== before[key];
  const at = (key: string) => clientPath(after.id, key);
  const crossed = isHttpTransport(after.transport) !== isHttpTransport(before.transport);
  const edits: YamlKeyEdit[] = [];
  if (changed('description')) edits.push({ path: at('description'), value: after.description || undefined });
  if (changed('enabled')) edits.push({ path: at('enabled'), value: after.enabled });
  // 取消勾选就删掉这个键，不留一个 false
  if (changed('share_sessions')) edits.push({ path: at('share_sessions'), value: after.share_sessions ? true : undefined });
  if (changed('transport')) edits.push({ path: at('transport'), value: after.transport });
  if (isHttpTransport(after.transport)) {
    if (crossed || changed('url')) edits.push({ path: at('url'), value: after.url });
    if (crossed || changed('headersText')) edits.push({ path: at('headers'), value: parseLooseKV(after.headersText) });
    if (crossed) for (const key of ['command', 'args', 'env']) edits.push({ path: at(key), value: undefined });
  } else {
    if (crossed || changed('command')) edits.push({ path: at('command'), value: after.command });
    if (crossed || changed('argsText')) edits.push({ path: at('args'), value: mcpArgs(after.argsText) });
    if (crossed || changed('envText')) edits.push({ path: at('env'), value: parseLooseKV(after.envText) });
    if (crossed) for (const key of ['url', 'headers']) edits.push({ path: at(key), value: undefined });
  }
  return edits;
}

/** 改一个 Client：它在打开之后被别处改过就报冲突；其余 Client 一字不动。 */
export function mcpClientUpdateEdits(raw: string, loaded: McpClientFormState, form: McpClientFormState): YamlKeyEdit[] {
  const spec = mcpClientMap(raw)[loaded.id];
  if (!spec) throw new Error('未在 MCP YAML 中找到此 Client。');
  if (itemChangedSince(loaded, { original: spec })) throw new StaleKeysError([`clients.${loaded.id}`]);
  return mcpClientFieldEdits(loaded, form);
}

export function mcpClientCreateEdits(raw: string, form: McpClientFormState): YamlKeyEdit[] {
  if (mcpClientMap(raw)[form.id]) throw new Error('该 MCP Client 已存在');
  return [{ path: clientPath(form.id), value: newMcpClientSpec(form) }];
}

export function mcpClientRemoveEdits(raw: string, id: string): YamlKeyEdit[] {
  mcpClientMap(raw);
  return [{ path: clientPath(id), value: undefined }];
}

const FRONTMATTER = /^---(\r?\n)([\s\S]*?)\r?\n---(\r?\n|$)/;

interface SkillParts {
  eol: string;
  meta: string;
  /** 收尾那行「---」后面跟的换行，文件到此为止时是空串。 */
  close: string;
  body: string;
}

function splitFrontmatter(raw: string): SkillParts {
  const match = raw.match(FRONTMATTER);
  if (!match) throw new Error('技能文件缺少 YAML frontmatter，请修复后再保存。');
  return { eol: match[1], meta: match[2], close: match[3], body: raw.slice(match[0].length) };
}

export function parseSkillQuickFields(raw: string): SkillQuickFields {
  const { meta } = splitFrontmatter(raw);
  const parsed = yaml.load(meta);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('技能 frontmatter 必须是对象。');
  const value = parsed as Record<string, unknown>;
  return {
    enabled: parseBoolFlag(value.enabled, true),
    strategy: String(value.strategy || 'normal'),
    keywords: Array.isArray(value.keywords) ? value.keywords.map(String).join(', ') : String(value.keywords || ''),
    order: String(value.order ?? 0),
    priority: String(value.priority ?? 0),
    scan_depth: String(value.scan_depth ?? 0),
  };
}

/** 只改 frontmatter 里和原文不一样的那几项；别的键、注释和正文原样留着。 */
export function serializeSkillQuickFields(raw: string, fields: SkillQuickFields): YamlRewrite {
  const current = parseSkillQuickFields(raw);
  const { eol, meta, close, body } = splitFrontmatter(raw);
  const edits: YamlKeyEdit[] = [];
  if (fields.enabled !== current.enabled) edits.push({ path: ['enabled'], value: fields.enabled });
  if (fields.strategy !== current.strategy) edits.push({ path: ['strategy'], value: fields.strategy || 'normal' });
  if (fields.keywords !== current.keywords) {
    edits.push({ path: ['keywords'], value: fields.keywords.split(',').map((item) => item.trim()).filter(Boolean) });
  }
  for (const key of ['order', 'priority', 'scan_depth'] as const) {
    if (fields[key] === current[key]) continue;
    const value = Number(fields[key] || 0);
    if (!Number.isFinite(value)) throw new Error(`${key} 必须是数字`);
    edits.push({ path: [key], value });
  }
  if (!edits.length) return { content: raw, preserved: true, note: '' };
  const rewritten = editYamlKeys(meta, edits);
  // 整份重写的兜底会带一个结尾换行，收尾的「---」前那个换行由这里补。
  const frontmatter = rewritten.content.replace(/\r?\n$/, '');
  return { ...rewritten, content: `---${eol}${frontmatter}${eol}---${close}${body}` };
}

/** 原文编辑器的语法自检。返回空串表示能解析，否则是解析器给出的报错。 */
export function hasLikelyYamlSyntaxIssue(value: string): string {
  try { yaml.load(value); return ''; } catch (error) { return error instanceof Error ? error.message : 'YAML 语法错误'; }
}
