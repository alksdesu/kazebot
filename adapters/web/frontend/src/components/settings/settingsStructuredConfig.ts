// Structured config helpers for Settings UI — powered by js-yaml.
// Replaces the former hand-rolled regex/recursive-descent parser with
// a proper YAML library while preserving every exported type and function
// signature so that consuming components need zero changes.
import yaml from 'js-yaml';

import { upsertYamlNested } from '../../console/nodeYaml';

// ==================== Type Definitions ====================

export type ScalarValue = string | number | boolean | null;
export type SimpleYamlValue = ScalarValue | SimpleYamlObject | SimpleYamlValue[];
export interface SimpleYamlObject { [key: string]: SimpleYamlValue; }

export interface ParsedProvidersResult {
  providers: Record<string, SimpleYamlObject>;
  rawProviders: string;
}

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
  transport: 'stdio' | 'sse' | 'streamable_http';
  command: string;
  argsText: string;
  envText: string;
  url: string;
  headersText: string;
}

export interface SkillFormState {
  name: string;
  description: string;
  enabled: boolean;
  strategy: 'normal' | 'constant';
  keywordsText: string;
  order: string;
  priority: string;
  scan_depth: string;
  body: string;
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
  tool_mode: EngineToolMode;
  max_workers: string;
  compact_threshold_tokens: string;
  compact_hard_threshold_tokens: string;
  compact_keep_recent_tokens: string;
  compact_keep_recent: string;
}

// ==================== Core Helpers ====================

// 不设 noCompatMode：这些文件都由 PyYAML（YAML 1.1）读回，裸写的 on/off/yes/no 会变成布尔、
// 12:30 会变成 750。留着 js-yaml 的兼容引号，两侧才是同一个值。
const DUMP_OPTS: yaml.DumpOptions = {
  indent: 2,
  lineWidth: -1,
  noRefs: true,
  sortKeys: false,
  quotingType: '"',
  forceQuotes: false,
};

function safeLoad(raw: string): Record<string, any> {
  const result = yaml.load(raw);
  if (result === undefined || result === null) return {};
  if (typeof result !== 'object' || Array.isArray(result)) throw new Error('YAML 顶层必须是映射，原文件未修改');
  return result as Record<string, any>;
}

function safeDump(obj: any): string {
  return yaml.dump(obj, DUMP_OPTS);
}

function str(value: any, fallback = ''): string {
  if (value === null || value === undefined) return fallback;
  return String(value);
}

function normalizeEngineToolMode(value: string, fallback: EngineToolMode = 'fake-native'): EngineToolMode {
  const normalized = value.trim().toLowerCase().replace(/_/g, '-');
  return normalized === 'native' || normalized === 'json' || normalized === 'fake-native' ? normalized : fallback;
}

/** 读嵌套路径，缺任一层返回 undefined。 */
function nested(doc: Record<string, any>, path: readonly string[]): any {
  let cur: any = doc;
  for (const key of path) {
    if (!cur || typeof cur !== 'object' || Array.isArray(cur)) return undefined;
    cur = cur[key];
  }
  return cur;
}

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

// ==================== Node Config ====================

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
    persistent: doc.persistent === true,
    prompt: Array.isArray(doc.prompt) ? JSON.stringify(doc.prompt, null, 2) : str(doc.prompt),
    promptIsStructured: Array.isArray(doc.prompt),
    delegate_targetsText: Array.isArray(doc.delegate_targets) ? doc.delegate_targets.map(String).join(', ') : '',
  };
}

export function serializeNodeConfig(raw: string, form: NodeConfigFormState): string {
  const doc = safeLoad(raw);
  doc.id = form.id.trim();
  doc.name = form.name.trim();
  doc.description = form.description.trim();
  doc.type = form.type;
  doc.model = form.model.trim();
  if (form.provider.trim()) doc.provider = form.provider.trim(); else delete doc.provider;
  if (form.memory_book.trim()) doc.memory_book = form.memory_book.trim(); else delete doc.memory_book;
  doc.persistent = form.persistent;
  if (form.prompt.trim()) {
    if (form.promptIsStructured) {
      const prompt = JSON.parse(form.prompt);
      if (!Array.isArray(prompt)) throw new Error('结构化提示词必须是 JSON 数组，原文件未修改');
      doc.prompt = prompt;
    } else doc.prompt = form.prompt;
  } else delete doc.prompt;
  doc.delegate_targets = commaTextToItems(form.delegate_targetsText);
  // tool_access 只能改 YAML 原文；这里整份 dump 会把 mode 对应不上的那半
  // （allowlist 下的 deny）连同注释一起洗掉。
  return safeDump(doc);
}

// ==================== Runtime Config ====================

// runtime.yaml 里这三项都在二级键下；曾经按顶层读写，于是表单永远空值，
// 保存还会注入三个无人消费的顶层键。
const RUNTIME_ENTRY_NODE_PATH = ['shell', 'entry_node_id'] as const;
const RUNTIME_TOOL_MODE_PATH = ['engine', 'tool_mode'] as const;
const RUNTIME_MAX_WORKERS_PATH = ['engine', 'max_workers'] as const;
const RUNTIME_COMPACT_THRESHOLD_PATH = ['engine', 'compact', 'threshold_tokens'] as const;
const RUNTIME_COMPACT_HARD_THRESHOLD_PATH = ['engine', 'compact', 'hard_threshold_tokens'] as const;
const RUNTIME_COMPACT_KEEP_TOKENS_PATH = ['engine', 'compact', 'keep_recent_tokens'] as const;
const RUNTIME_COMPACT_KEEP_SEGMENTS_PATH = ['engine', 'compact', 'keep_recent'] as const;

/** 数字键读成输入框文本。缺键读空串，表示「不覆盖代码默认值」。 */
function numText(doc: Record<string, any>, path: readonly string[]): string {
  const value = nested(doc, path);
  return value === undefined || value === null ? '' : String(value);
}

/** 整数输入框回写成 yaml 标量。空串与非法值都返回 ''，由调用方当作删键。 */
function intText(value: string, min = 0): string {
  const parsed = Number.parseInt(value.trim(), 10);
  return Number.isFinite(parsed) && parsed >= min ? String(parsed) : '';
}

export function parseRuntimeConfig(raw: string): RuntimeConfigFormState {
  const doc = safeLoad(raw);
  return {
    entry_node_id: str(nested(doc, RUNTIME_ENTRY_NODE_PATH)),
    tool_mode: normalizeEngineToolMode(str(nested(doc, RUNTIME_TOOL_MODE_PATH), 'fake-native')),
    max_workers: numText(doc, RUNTIME_MAX_WORKERS_PATH),
    compact_threshold_tokens: numText(doc, RUNTIME_COMPACT_THRESHOLD_PATH),
    compact_hard_threshold_tokens: numText(doc, RUNTIME_COMPACT_HARD_THRESHOLD_PATH),
    compact_keep_recent_tokens: numText(doc, RUNTIME_COMPACT_KEEP_TOKENS_PATH),
    compact_keep_recent: numText(doc, RUNTIME_COMPACT_KEEP_SEGMENTS_PATH),
  };
}

export function serializeRuntimeConfig(raw: string, form: RuntimeConfigFormState): string {
  // 空基底会让整份 runtime.yaml 被这几个键的输出替换掉，而 safeLoad 对空串是静默返回 {}。
  if (!raw.trim()) throw new Error('尚未读入 runtime.yaml，拒绝用空内容覆盖');
  // 逐键定点改写而不是 load/dump 一轮：runtime.yaml 里一百多行说明注释全靠原文留着。
  safeLoad(raw);
  let out = raw;
  out = upsertYamlNested(out, [...RUNTIME_ENTRY_NODE_PATH], form.entry_node_id.trim());
  out = upsertYamlNested(out, [...RUNTIME_TOOL_MODE_PATH], normalizeEngineToolMode(form.tool_mode));
  // 留空表示不覆盖默认值；写 0 会让 engine 一个 worker 都不起。
  out = upsertYamlNested(out, [...RUNTIME_MAX_WORKERS_PATH], intText(form.max_workers, 1));
  // 0 各有含义：软阈值 0 关掉自动压缩，硬阈值 0 取软阈值的 1.25 倍，保留量 0 改按段数算。
  out = upsertYamlNested(out, [...RUNTIME_COMPACT_THRESHOLD_PATH], intText(form.compact_threshold_tokens));
  out = upsertYamlNested(out, [...RUNTIME_COMPACT_HARD_THRESHOLD_PATH], intText(form.compact_hard_threshold_tokens));
  out = upsertYamlNested(out, [...RUNTIME_COMPACT_KEEP_TOKENS_PATH], intText(form.compact_keep_recent_tokens));
  out = upsertYamlNested(out, [...RUNTIME_COMPACT_KEEP_SEGMENTS_PATH], intText(form.compact_keep_recent, 2));
  return out;
}

// ==================== Providers ====================

export function parseProvidersFromRuntime(raw: string): ParsedProvidersResult {
  const doc = safeLoad(raw);
  const src = doc.providers;
  const providers: Record<string, SimpleYamlObject> = {};
  if (src && typeof src === 'object' && !Array.isArray(src)) {
    for (const [k, v] of Object.entries(src)) {
      if (v && typeof v === 'object' && !Array.isArray(v)) providers[k] = v as SimpleYamlObject;
    }
  }
  const rawBlock = Object.keys(providers).length > 0
    ? yaml.dump({ providers }, DUMP_OPTS).trimEnd()
    : 'providers: {}';
  return { providers, rawProviders: rawBlock };
}

export function serializeProvidersBlock(providers: Record<string, SimpleYamlObject>): string {
  const filtered = Object.fromEntries(Object.entries(providers).filter(([k]) => k.trim()));
  if (Object.keys(filtered).length === 0) return 'providers: {}';
  return yaml.dump({ providers: filtered }, DUMP_OPTS).trimEnd();
}

export function replaceProvidersInRuntime(raw: string, providers: Record<string, SimpleYamlObject>): string {
  const doc = safeLoad(raw);
  doc.providers = providers;
  return safeDump(doc);
}

// ==================== Schedules ====================

export function parseSchedules(raw: string): ScheduleFormState[] {
  const doc = safeLoad(raw);
  const list = doc.schedules === undefined ? [] : doc.schedules;
  if (!Array.isArray(list) || list.some(item => !item || typeof item !== 'object' || Array.isArray(item))) throw new Error('schedules 必须是对象数组，原文件未修改');
  if (list.some(item => item.type !== undefined && !['message', 'script'].includes(item.type))) throw new Error('存在不支持的定时任务类型，请用原文编辑');
  return list
    .map((item: Record<string, any>) => ({
      original: { ...item },
      id: str(item.id),
      cron: str(item.cron),
      type: (item.type === 'script' ? 'script' : 'message') as 'message' | 'script',
      text: str(item.text),
      command: str(item.command),
      enabled: item.enabled !== false,
      once: item.once === true,
      conversation_key: str(item.conversation_key),
      entry_node_id: str(item.entry_node_id),
      workflow_id: str(item.workflow_id),
      timeout: item.timeout === undefined ? '' : String(item.timeout),
      silent: item.silent !== false,
    }));
}

export function serializeSchedules(schedules: ScheduleFormState[], raw = ''): string {
  parseSchedules(raw);
  const items = schedules.map((s) => {
    const obj: Record<string, any> = {
      ...s.original,
      id: s.id, cron: s.cron, type: s.type, text: s.text,
      enabled: s.enabled, once: s.once,
    };
    for (const key of ['conversation_key', 'entry_node_id', 'workflow_id'] as const) {
      if (s[key]) obj[key] = s[key]; else delete obj[key];
    }
    if (s.type === 'script') {
      obj.command = s.command;
      if (s.timeout) obj.timeout = Number(s.timeout) || s.timeout; else delete obj.timeout;
      obj.silent = s.silent;
    } else {
      delete obj.command; delete obj.timeout; delete obj.silent;
    }
    return obj;
  });
  return yaml.dump({ ...safeLoad(raw), schedules: items }, DUMP_OPTS);
}

// ==================== MCP Clients ====================

export function parseMcpClients(raw: string): McpClientFormState[] {
  const doc = safeLoad(raw);
  const clients = doc.clients;
  if (clients === undefined) return [];
  if (!clients || typeof clients !== 'object' || Array.isArray(clients) || Object.values(clients).some(v => !v || typeof v !== 'object' || Array.isArray(v))) throw new Error('clients 必须是对象映射，原文件未修改');
  return Object.entries(clients)
    .map(([id, v]) => {
      const c = v as Record<string, any>;
      if (c.transport !== undefined && !['stdio', 'sse', 'streamable_http'].includes(c.transport)) throw new Error('存在不支持的 MCP 传输方式，请用原文编辑');
      return {
        original: { ...c },
        id,
        description: str(c.description),
        enabled: c.enabled !== false,
        transport: (c.transport === 'sse' ? 'sse' : c.transport === 'stdio' ? 'stdio' : 'streamable_http') as 'stdio' | 'sse' | 'streamable_http',
        command: str(c.command),
        argsText: Array.isArray(c.args) ? JSON.stringify(c.args) : '[]',
        envText: serializeLooseKV(c.env),
        url: str(c.url),
        headersText: serializeLooseKV(c.headers),
      };
    });
}

export function serializeMcpClients(clients: McpClientFormState[], raw = ''): string {
  parseMcpClients(raw);
  const obj: Record<string, any> = {};
  for (const c of clients) {
    const entry: Record<string, any> = {
      ...c.original,
      transport: c.transport,
      enabled: c.enabled,
      description: c.description,
    };
    if (c.transport === 'stdio') {
      entry.command = c.command;
      const args = c.argsText.trim().startsWith('[') ? JSON.parse(c.argsText) : c.argsText.split('\n').map(s => s.trim()).filter(Boolean);
      if (!Array.isArray(args) || args.some(item => typeof item !== 'string')) throw new Error('MCP 启动参数必须是字符串数组');
      entry.args = args;
      entry.env = c.original && c.envText === serializeLooseKV(c.original.env) ? c.original.env : parseLooseKV(c.envText);
      delete entry.url; delete entry.headers;
    } else {
      entry.url = c.url;
      entry.headers = c.original && c.headersText === serializeLooseKV(c.original.headers) ? c.original.headers : parseLooseKV(c.headersText);
      delete entry.command; delete entry.args; delete entry.env;
    }
    obj[c.id] = entry;
  }
  return yaml.dump({ version: 1, ...safeLoad(raw), clients: obj }, DUMP_OPTS);
}

// ==================== Skills ====================

export function parseSkillMarkdown(raw: string, fallbackName: string): SkillFormState {
  let meta: Record<string, any> = {};
  let body = raw;
  if (raw.startsWith('---\n')) {
    const end = raw.indexOf('\n---\n', 4);
    if (end >= 0) {
      try {
        const parsed = yaml.load(raw.slice(4, end));
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) meta = parsed as Record<string, any>;
      } catch { /* ignore frontmatter parse errors */ }
      body = raw.slice(end + 5);
    }
  }
  const keywords = Array.isArray(meta.keywords) ? meta.keywords.map(String) : [];
  return {
    name: str(meta.name, fallbackName),
    description: str(meta.description),
    enabled: meta.enabled !== false,
    strategy: meta.strategy === 'constant' ? 'constant' : 'normal',
    keywordsText: keywords.join('\n'),
    order: meta.order === undefined ? '0' : String(meta.order),
    priority: meta.priority === undefined ? '0' : String(meta.priority),
    scan_depth: meta.scan_depth === undefined ? '0' : String(meta.scan_depth),
    body,
  };
}

export function serializeSkillMarkdown(form: SkillFormState): string {
  const keywords = form.keywordsText.split('\n').map((s) => s.trim()).filter(Boolean);
  const meta: Record<string, any> = {
    name: form.name,
    description: form.description,
    enabled: form.enabled,
    strategy: form.strategy,
    keywords,
    order: Number(form.order) || 0,
    priority: Number(form.priority) || 0,
    scan_depth: Number(form.scan_depth) || 0,
  };
  const frontmatter = yaml.dump(meta, DUMP_OPTS);
  return `---\n${frontmatter}---\n\n${form.body.replace(/^\n+/, '')}`;
}
