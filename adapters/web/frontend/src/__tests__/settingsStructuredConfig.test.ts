// 设置页表单和 yaml 原文之间的转换：只写表单改过的键，注释、键序和表单不认识的键都照旧。
import { describe, expect, it } from 'vitest';

import type { McpClientFormState, RuntimeConfigFormState, ScheduleFormState } from '../components/settings/settingsStructuredConfig';

import {
  mcpClientCreateEdits,
  mcpClientRemoveEdits,
  mcpClientUpdateEdits,
  parseBoolFlag,
  parseMcpClients,
  parseNodeConfig,
  parseRuntimeConfig,
  parseSchedules,
  parseSkillQuickFields,
  scheduleCreateEdits,
  scheduleRemoveEdits,
  scheduleUpdateEdits,
  serializeNodeConfig,
  serializeRuntimeConfig,
  serializeSkillQuickFields,
} from '../components/settings/settingsStructuredConfig';
import { StaleKeysError } from '../console/yamlCommit';
import { editYamlKeys } from '../console/yamlRewrite';

// tool_access 归「工具与权限 → 节点授权」独家管，结构化表单只能原样带过去。
// 以前它跟着 mode 整份重写 tool_access，从 all 切到 allowlist 会顺手抹掉 deny。
describe('node config keeps tool_access untouched', () => {
  const ALLOWLIST_NODE = [
    'id: qq.orchestrator',
    'name: QQ 综合入口',
    'type: ai',
    'tool_access:',
    '  mode: allowlist',
    '  allow:',
    '    - web_search',
    '    - qq_forward',
    '',
  ].join('\n');

  it('carries the allow list through a load-then-save round trip', () => {
    const { content: saved } = serializeNodeConfig(ALLOWLIST_NODE, parseNodeConfig(ALLOWLIST_NODE));

    expect(saved).toContain('mode: allowlist');
    expect(saved).toContain('- web_search');
    expect(saved).toContain('- qq_forward');
  });

  it('keeps deny under mode all instead of dropping it', () => {
    const raw = ['id: bootstrap.executor', 'tool_access:', '  mode: all',
      '  deny:', '    - execute_command', ''].join('\n');

    const { content: saved } = serializeNodeConfig(raw, parseNodeConfig(raw));

    expect(saved).toContain('mode: all');
    expect(saved).toContain('- execute_command');
  });

  it('still writes the fields it does own', () => {
    const { content: saved } = serializeNodeConfig(ALLOWLIST_NODE, {
      ...parseNodeConfig(ALLOWLIST_NODE),
      name: '改过的名字',
    });

    expect(saved).toContain('改过的名字');
    expect(saved).toContain('mode: allowlist');
  });

  // 表单每敲一个字就写一次原文：多打的空格这类写回去没区别的改动，不能把原文改出差别。
  it('leaves the text alone when the form only differs by whitespace the file would trim', () => {
    const raw = 'id: qq.orchestrator\nname: QQ\n# 提示词\nprompt: |\n  你好\n  第二行\n';

    expect(serializeNodeConfig(raw, { ...parseNodeConfig(raw), name: 'QQ ' }).content).toBe(raw);
  });

  it('keeps a block scalar prompt as a block scalar when it grows', () => {
    const raw = 'id: qq.orchestrator\nname: QQ\n# 提示词\nprompt: |\n  你好\n  第二行\ntool_access:\n  mode: allowlist\n';
    const form = parseNodeConfig(raw);

    const { content, preserved } = serializeNodeConfig(raw, { ...form, prompt: `${form.prompt}第三行\n` });

    expect(content).toBe('id: qq.orchestrator\nname: QQ\n# 提示词\nprompt: |\n  你好\n  第二行\n  第三行\ntool_access:\n  mode: allowlist\n');
    expect(preserved).toBe(true);
  });
});

describe('runtime config key paths', () => {
  const RUNTIME = [
    'version: 1',
    'engine:',
    '  tool_mode: json',
    '  max_workers: 4',
    'shell:',
    '  entry_node_id: bootstrap.shell_orchestrator',
    '',
  ].join('\n');

  const form = (over: Partial<RuntimeConfigFormState> = {}): RuntimeConfigFormState => ({
    entry_node_id: '',
    tool_mode: '',
    max_workers: '',
    compact_threshold_tokens: '',
    compact_hard_threshold_tokens: '',
    compact_keep_recent_tokens: '',
    compact_keep_recent: '',
    ...over,
  });

  it('reads the nested keys the engine actually consumes', () => {
    const parsed = parseRuntimeConfig(RUNTIME);

    expect(parsed.entry_node_id).toBe('bootstrap.shell_orchestrator');
    expect(parsed.tool_mode).toBe('json');
    expect(parsed.max_workers).toBe('4');
  });

  it('writes back into the nested keys without inventing top-level ones', () => {
    const { content: saved } = serializeRuntimeConfig(RUNTIME, form({
      entry_node_id: 'qq.orchestrator', tool_mode: 'native', max_workers: '8',
    }));

    expect(saved).toBe('version: 1\nengine:\n  tool_mode: native\n  max_workers: 8\nshell:\n  entry_node_id: qq.orchestrator\n');
  });

  // 只换 entry_node_id 的一份写回，其余键给成和原文一致的值。
  const withEntryNode = (id: string): string => serializeRuntimeConfig(RUNTIME, form({
    entry_node_id: id, tool_mode: 'json', max_workers: '4',
  })).content;

  const replacingEntryNode = (line: string): string => RUNTIME.replace('entry_node_id: bootstrap.shell_orchestrator', line);

  // runtime.yaml 由 PyYAML（YAML 1.1）读回：裸写的 no 会变成布尔、12 变成数字、# 后面被当注释切掉，
  // 节点 id 就此对不上任何节点。
  it('quotes an entry node id that a YAML 1.1 reader would fold', () => {
    expect(withEntryNode('no')).toBe(replacingEntryNode('entry_node_id: "no"'));
    expect(withEntryNode('12')).toBe(replacingEntryNode('entry_node_id: "12"'));
    expect(withEntryNode('a # b')).toBe(replacingEntryNode('entry_node_id: "a # b"'));
  });

  it('leaves an ordinary entry node id unquoted', () => {
    expect(withEntryNode('qq.orchestrator')).toBe(replacingEntryNode('entry_node_id: qq.orchestrator'));
  });

  it('reads a quoted entry node id back as the text that was typed', () => {
    expect(parseRuntimeConfig(withEntryNode('a # b')).entry_node_id).toBe('a # b');
    expect(parseRuntimeConfig(withEntryNode('no')).entry_node_id).toBe('no');
  });

  // 实例层还没有自己那份时从空文档开始：只落改过的键，其余跟着仓库默认走，仓库以后改默认值这个实例也拿得到。
  it('writes only the edited keys onto an empty instance layer', () => {
    const { content, preserved } = serializeRuntimeConfig('', form({ tool_mode: 'native', max_workers: '8' }));

    expect(content).toBe('engine:\n  tool_mode: native\n  max_workers: 8\n');
    expect(preserved).toBe(true);
  });

  it('writes nothing when the form matches an empty instance layer', () => {
    expect(serializeRuntimeConfig('', form()).content).toBe('');
  });

  it('reads a missing tool mode as following the repository default', () => {
    expect(parseRuntimeConfig('').tool_mode).toBe('');
    expect(parseRuntimeConfig('engine:\n  max_workers: 2\n').tool_mode).toBe('');
  });

  it('drops the instance tool mode when the form goes back to following the default', () => {
    const raw = 'engine:\n  tool_mode: json  # 默认\n  max_workers: 4\n';

    expect(serializeRuntimeConfig(raw, { ...parseRuntimeConfig(raw), tool_mode: '' }).content).toBe('engine:\n  max_workers: 4\n');
  });

  it('keeps every key the form does not own', () => {
    const full = [
      'version: 1',
      'engine:',
      '  tool_mode: json',
      '  max_workers: 4',
      '  max_steps: 64',
      'providers:',
      '  openai:',
      '    timeout_sec: 60',
      'shell:',
      '  entry_node_id: bootstrap.shell_orchestrator',
      '',
    ].join('\n');

    const { content: saved } = serializeRuntimeConfig(full, form({
      entry_node_id: 'qq.orchestrator', tool_mode: 'native', max_workers: '8',
    }));

    expect(saved).toContain('version: 1');
    expect(saved).toContain('max_steps: 64');
    expect(saved).toContain('providers:');
    expect(saved).toContain('timeout_sec: 60');
  });

  it('accepts the underscore spelling the engine also normalizes', () => {
    expect(parseRuntimeConfig('engine:\n  tool_mode: fake_native\n').tool_mode).toBe('fake-native');
  });

  it('leaves max_workers unset rather than writing 0', () => {
    const { content: saved } = serializeRuntimeConfig(RUNTIME, form({ entry_node_id: 'x', tool_mode: 'json' }));

    expect(saved).toBe('version: 1\nengine:\n  tool_mode: json\nshell:\n  entry_node_id: x\n');
  });

  it('creates the parent block when runtime.yaml lacks it', () => {
    const { content: saved } = serializeRuntimeConfig('version: 1\n', form({
      entry_node_id: 'qq.orchestrator', tool_mode: 'json', max_workers: '2',
    }));

    expect(parseRuntimeConfig(saved).entry_node_id).toBe('qq.orchestrator');
    expect(parseRuntimeConfig(saved).max_workers).toBe('2');
  });

  // engine.compact 决定何时压缩历史，之前只能手写 YAML。
  describe('compact thresholds', () => {
    const COMMENTED = [
      '# Clonoth runtime tuning config',
      'version: 1',
      '',
      'engine:',
      '  max_steps: 64            # 单轮最多几步',
      '',
      '  compact:',
      '    threshold_tokens: 256000   # 软阈值：超过此值在后台静默压缩',
      '    hard_threshold_tokens: 0   # 0 = 取软阈值的 1.25 倍',
      '    keep_recent_tokens: 30000  # 压缩时保留的原文 token 量',
      '    keep_recent: 6             # keep_recent_tokens 为 0 时生效',
      '',
      'shell:',
      '  entry_node_id: bootstrap.shell_orchestrator',
      '',
    ].join('\n');

    it('reads every compact key', () => {
      const parsed = parseRuntimeConfig(COMMENTED);

      expect(parsed.compact_threshold_tokens).toBe('256000');
      expect(parsed.compact_hard_threshold_tokens).toBe('0');
      expect(parsed.compact_keep_recent_tokens).toBe('30000');
      expect(parsed.compact_keep_recent).toBe('6');
    });

    it('writes new values back into the nested keys', () => {
      const { content: saved } = serializeRuntimeConfig(COMMENTED, form({
        entry_node_id: 'bootstrap.shell_orchestrator',
        compact_threshold_tokens: '120000',
        compact_hard_threshold_tokens: '150000',
        compact_keep_recent_tokens: '20000',
        compact_keep_recent: '4',
      }));

      const parsed = parseRuntimeConfig(saved);
      expect(parsed.compact_threshold_tokens).toBe('120000');
      expect(parsed.compact_hard_threshold_tokens).toBe('150000');
      expect(parsed.compact_keep_recent_tokens).toBe('20000');
      expect(parsed.compact_keep_recent).toBe('4');
    });

    // load/dump 一轮会把整份文件的注释抹掉，等于每存一次就吃掉一次文档。
    it('keeps both standalone and trailing comments', () => {
      const { content: saved } = serializeRuntimeConfig(COMMENTED, {
        ...parseRuntimeConfig(COMMENTED),
        compact_threshold_tokens: '120000',
      });

      expect(saved).toContain('# Clonoth runtime tuning config');
      expect(saved).toContain('max_steps: 64            # 单轮最多几步');
      expect(saved).toContain('threshold_tokens: 120000   # 软阈值：超过此值在后台静默压缩');
    });

    it('keeps 0 rather than treating it as unset', () => {
      const { content: saved } = serializeRuntimeConfig(COMMENTED, {
        ...parseRuntimeConfig(COMMENTED),
        compact_threshold_tokens: '0',
      });

      expect(parseRuntimeConfig(saved).compact_threshold_tokens).toBe('0');
    });

    it('builds the compact block when runtime.yaml has none', () => {
      const { content: saved } = serializeRuntimeConfig('engine:\n  max_steps: 64\n', form({
        compact_threshold_tokens: '90000',
      }));

      expect(parseRuntimeConfig(saved).compact_threshold_tokens).toBe('90000');
      expect(saved).toContain('max_steps: 64');
    });

    it('drops the key when the field is cleared', () => {
      const { content: saved } = serializeRuntimeConfig(COMMENTED, form({
        entry_node_id: 'bootstrap.shell_orchestrator',
      }));

      expect(saved).not.toContain('threshold_tokens');
      expect(saved).toContain('# Clonoth runtime tuning config');
    });
  });
});

describe('schedules are edited item by item', () => {
  const SCHEDULES = [
    'schedules:',
    '  # 早安',
    '  - id: morning',
    '    cron: "0 8 * * *"',
    '    text: 早',
    '  - id: night',
    '    cron: "0 23 * * *"',
    '    text: 晚',
    '',
  ].join('\n');

  const fresh = (id: string): ScheduleFormState => ({
    id, cron: '0 0 * * *', type: 'message', text: '', command: '', enabled: false, once: false,
    conversation_key: '', entry_node_id: '', workflow_id: '', timeout: '', silent: true,
  });

  it('parses the editable fields', () => {
    const [demo] = parseSchedules('schedules:\n- id: demo\n  cron: "*/5 * * * *"\n  type: script\n  command: python task.py\n  enabled: false\n  once: true\n  text: "运行脚本"\n');

    expect(demo).toMatchObject({ id: 'demo', type: 'script', command: 'python task.py', enabled: false, once: true, text: '运行脚本' });
  });

  it('treats an empty schedules key like the scheduler does: no tasks', () => {
    expect(parseSchedules('schedules:\n')).toEqual([]);
    expect(parseSchedules('')).toEqual([]);
  });

  // 以前保存一项要把整张表重新 dump：注释没了，没动过的任务也被补上一堆默认键。
  it('writes only the changed field of the changed item', () => {
    const [morning] = parseSchedules(SCHEDULES);

    const { content, preserved } = editYamlKeys(SCHEDULES, scheduleUpdateEdits(SCHEDULES, morning, { ...morning, text: '早安' }));

    expect(content).toBe(SCHEDULES.replace('text: 早\n', 'text: 早安\n'));
    expect(preserved).toBe(true);
  });

  it('adds the script fields only when the type turns into script', () => {
    const raw = 'schedules:\n  - id: morning\n    cron: "0 8 * * *"\n    text: 早\n';
    const [morning] = parseSchedules(raw);

    const { content } = editYamlKeys(raw, scheduleUpdateEdits(raw, morning, { ...morning, type: 'script', command: 'python a.py', timeout: '30' }));

    expect(content).toBe('schedules:\n  - id: morning\n    cron: "0 8 * * *"\n    text: 早\n    type: script\n    command: python a.py\n    timeout: 30\n    silent: true\n');
  });

  it('refuses to overwrite an item that changed after the form opened', () => {
    const [morning] = parseSchedules(SCHEDULES);
    const changed = SCHEDULES.replace('text: 早\n', 'text: 别处改的\n');

    expect(() => scheduleUpdateEdits(changed, morning, { ...morning, enabled: false })).toThrow(StaleKeysError);
  });

  it('merges onto a file where only another item changed', () => {
    const [morning] = parseSchedules(SCHEDULES);
    const changed = SCHEDULES.replace('text: 晚\n', 'text: 别处改的\n');

    const { content } = editYamlKeys(changed, scheduleUpdateEdits(changed, morning, { ...morning, text: '早安' }));

    expect(content).toContain('text: 早安');
    expect(content).toContain('text: 别处改的');
  });

  it('reports an item that is gone instead of recreating it', () => {
    const [morning] = parseSchedules(SCHEDULES);

    expect(() => scheduleUpdateEdits('schedules: []\n', morning, morning)).toThrow('未在 schedules.yaml 中找到此定时任务。');
  });

  it('appends a new item after the last one without touching the others', () => {
    const { content, preserved } = editYamlKeys(SCHEDULES, scheduleCreateEdits(SCHEDULES, fresh('new_schedule')));

    expect(content).toBe(`${SCHEDULES}  - id: new_schedule\n    cron: 0 0 * * *\n    type: message\n    text: ""\n    enabled: false\n    once: false\n`);
    expect(preserved).toBe(true);
  });

  it('refuses to create an id that already exists', () => {
    expect(() => scheduleCreateEdits(SCHEDULES, fresh('night'))).toThrow('该任务 ID 已存在');
  });

  it('drops only the removed item', () => {
    const { content } = editYamlKeys(SCHEDULES, scheduleRemoveEdits(SCHEDULES, 'night'));

    expect(content).toBe('schedules:\n  # 早安\n  - id: morning\n    cron: "0 8 * * *"\n    text: 早\n');
  });

  // 这几份 yaml 都由 PyYAML（YAML 1.1）读回：裸写的 on/off/yes/no 变成布尔，12:30 变成 750。
  it('quotes a schedule field that reads as a boolean', () => {
    const raw = 'schedules:\n- id: demo\n  cron: "*/5 * * * *"\n  type: message\n  text: hi\n';
    const [demo] = parseSchedules(raw);

    const { content } = editYamlKeys(raw, scheduleUpdateEdits(raw, demo, { ...demo, text: 'off', conversation_key: 'y' }));

    expect(content).toBe('schedules:\n- id: demo\n  cron: "*/5 * * * *"\n  type: message\n  text: "off"\n  conversation_key: "y"\n');
  });
});

describe('MCP clients are edited client by client', () => {
  const CLIENTS = [
    'version: 1',
    '# 本地工具',
    'clients:',
    '  fs:',
    '    command: npx   # 没写 transport，后端按 stdio 起',
    '    args: ["-y", "server-fs"]',
    '  remote:',
    '    transport: http',
    '    url: https://mcp.example.test',
    '    headers:',
    '      Authorization: Bearer x',
    '',
  ].join('\n');

  const created = (id: string, over: Partial<McpClientFormState> = {}): McpClientFormState => ({
    id, description: '通过设置页面创建的 MCP Client。', enabled: false, share_sessions: false, transport: 'streamable_http',
    command: '', argsText: '', envText: '', url: '', headersText: '', ...over,
  });

  // 后端没写 transport 按 stdio 起，http / streamable-http 都算 streamable_http；表单跟着同一套认。
  it('reads the transport the way toolbox/mcp_runtime.py does', () => {
    const [fs, remote] = parseMcpClients(CLIENTS);

    expect(fs).toMatchObject({ id: 'fs', transport: 'stdio', argsText: '["-y","server-fs"]' });
    expect(remote).toMatchObject({ id: 'remote', transport: 'streamable_http', headersText: 'Authorization: Bearer x' });
    expect(parseMcpClients('clients:\n  a:\n    transport: streamable-http\n')[0].transport).toBe('streamable_http');
  });

  it('treats an empty clients key as no clients', () => {
    expect(parseMcpClients('version: 1\nclients:\n')).toEqual([]);
  });

  it('writes only the edited field and leaves the rest of the file as it was', () => {
    const [fs] = parseMcpClients(CLIENTS);

    const edits = mcpClientUpdateEdits(CLIENTS, fs, { ...fs, description: '文件系统' });
    const { content, preserved } = editYamlKeys(CLIENTS, edits);

    expect(edits).toEqual([{ path: ['clients', 'fs', 'description'], value: '文件系统' }]);
    expect(content).toBe(CLIENTS.replace('    args: ["-y", "server-fs"]\n', '    args: ["-y", "server-fs"]\n    description: 文件系统\n'));
    expect(preserved).toBe(true);
  });

  // 以前任何一次保存都会给每个没写 transport 的 stdio 客户端补上 streamable_http，顺手删掉 command 和 args。
  it('never touches a client that was not edited', () => {
    const [, remote] = parseMcpClients(CLIENTS);

    const { content } = editYamlKeys(CLIENTS, mcpClientUpdateEdits(CLIENTS, remote, { ...remote, enabled: false }));

    expect(content).toContain('    command: npx   # 没写 transport，后端按 stdio 起\n    args: ["-y", "server-fs"]\n');
    expect(content).not.toMatch(/fs:\n {4}transport/);
  });

  it('clears the other family of keys only when the transport crosses between stdio and http', () => {
    const [, remote] = parseMcpClients(CLIENTS);

    const toStdio = editYamlKeys(CLIENTS, mcpClientUpdateEdits(CLIENTS, remote, { ...remote, transport: 'stdio', command: 'uvx', argsText: '["srv"]' })).content;
    const toSse = editYamlKeys(CLIENTS, mcpClientUpdateEdits(CLIENTS, remote, { ...remote, transport: 'sse' })).content;

    expect(toStdio.slice(toStdio.indexOf('  remote:'))).toBe('  remote:\n    transport: stdio\n    command: uvx\n    args:\n      - srv\n    env: {}\n');
    expect(toSse.slice(toSse.indexOf('  remote:'))).toBe('  remote:\n    transport: sse\n    url: https://mcp.example.test\n    headers:\n      Authorization: Bearer x\n');
  });

  it('keeps commas and newlines inside JSON arguments', () => {
    const [fs] = parseMcpClients(CLIENTS);

    const edits = mcpClientUpdateEdits(CLIENTS, fs, { ...fs, argsText: '["one,two", "three\\nfour"]' });

    expect(edits).toEqual([{ path: ['clients', 'fs', 'args'], value: ['one,two', 'three\nfour'] }]);
  });

  it('rejects arguments that are not a list of strings', () => {
    const [fs] = parseMcpClients(CLIENTS);

    expect(() => mcpClientUpdateEdits(CLIENTS, fs, { ...fs, argsText: '["a", 1]' })).toThrow('MCP 启动参数必须是字符串数组');
    expect(() => mcpClientUpdateEdits(CLIENTS, fs, { ...fs, argsText: '["a",' })).toThrow('MCP 启动参数必须是 JSON 字符串数组');
  });

  it('refuses to overwrite a client that changed after the form opened', () => {
    const [fs] = parseMcpClients(CLIENTS);
    const changed = CLIENTS.replace('server-fs', 'server-other');

    expect(() => mcpClientUpdateEdits(changed, fs, { ...fs, description: 'x' })).toThrow(StaleKeysError);
  });

  it('reports a client that is gone instead of recreating it', () => {
    const [fs] = parseMcpClients(CLIENTS);

    expect(() => mcpClientUpdateEdits('version: 1\nclients: {}\n', fs, { ...fs, description: 'x' })).toThrow('未在 MCP YAML 中找到此 Client。');
  });

  it('creates a new client under clients and keeps the others byte for byte', () => {
    const { content } = editYamlKeys(CLIENTS, mcpClientCreateEdits(CLIENTS, created('new')));

    expect(content).toBe(`${CLIENTS}  new:\n    transport: streamable_http\n    enabled: false\n    description: 通过设置页面创建的 MCP Client。\n    url: ""\n    headers: {}\n`);
  });

  it('refuses to create a client id that exists', () => {
    expect(() => mcpClientCreateEdits(CLIENTS, created('fs'))).toThrow('该 MCP Client 已存在');
  });

  it('removes a client and collapses clients when it was the last one', () => {
    const raw = 'version: 1\nclients:\n  only:\n    command: x\n';

    expect(editYamlKeys(raw, mcpClientRemoveEdits(raw, 'only')).content).toBe('version: 1\n');
  });

  it('quotes env values a YAML 1.1 reader would fold, and leaves safe ones bare', () => {
    const raw = 'version: 1\nclients:\n  local:\n    transport: stdio\n    command: npx\n';
    const [local] = parseMcpClients(raw);

    const { content } = editYamlKeys(raw, mcpClientUpdateEdits(raw, local, { ...local, envText: 'STRICT: no\nMODE: on\nWINDOW: 12:30\nNAME: 早安' }));

    expect(content).toBe(`${raw}    env:\n      STRICT: "no"\n      MODE: "on"\n      WINDOW: "12:30"\n      NAME: 早安\n`);
  });

  // 文件里没有 share_sessions 就是按对话隔离；后端只在为真时写这个键。
  describe('share_sessions', () => {
    const shares = (value: string): boolean => (
      parseMcpClients(`clients:\n  a:\n    command: x\n    share_sessions: ${value}\n`)[0].share_sessions
    );

    // 和 clonoth_runtime.parse_bool 认同一套写法，认不出的、没写的都算不共用。
    it('reads the value the way the backend parses switches', () => {
      for (const value of ['true', 'True', 'yes', 'on', 'Y', '1', '"TRUE"', '" On "']) expect(shares(value)).toBe(true);
      for (const value of ['false', 'no', 'off', '0', 'sometimes', '""', '~']) expect(shares(value)).toBe(false);
      expect(parseMcpClients('clients:\n  a:\n    command: x\n')[0].share_sessions).toBe(false);
    });

    it('writes share_sessions: true when the box is ticked and leaves the rest of the file as it was', () => {
      const [fs] = parseMcpClients(CLIENTS);

      const edits = mcpClientUpdateEdits(CLIENTS, fs, { ...fs, share_sessions: true });
      const { content, preserved } = editYamlKeys(CLIENTS, edits);

      expect(edits).toEqual([{ path: ['clients', 'fs', 'share_sessions'], value: true }]);
      expect(content).toBe(CLIENTS.replace('    args: ["-y", "server-fs"]\n', '    args: ["-y", "server-fs"]\n    share_sessions: true\n'));
      expect(preserved).toBe(true);
    });

    // 取消勾选删掉这个键，不留一个 false；表单不认识的键原样留着。
    it('removes the key when the box is unticked and keeps keys the form does not know', () => {
      const raw = 'version: 1\nclients:\n  fs:\n    command: npx\n    share_sessions: yes   # 无状态的服务才共用\n    restart_hint: weekly\n';
      const [fs] = parseMcpClients(raw);
      expect(fs.share_sessions).toBe(true);

      const edits = mcpClientUpdateEdits(raw, fs, { ...fs, share_sessions: false });

      expect(edits).toHaveLength(1);
      expect(edits[0].path).toEqual(['clients', 'fs', 'share_sessions']);
      expect(edits[0].value).toBeUndefined();
      expect(editYamlKeys(raw, edits).content).toBe('version: 1\nclients:\n  fs:\n    command: npx\n    restart_hint: weekly\n');
    });

    it('leaves a hand-written value alone when another field is edited', () => {
      const raw = 'version: 1\nclients:\n  fs:\n    command: npx\n    share_sessions: on\n';
      const [fs] = parseMcpClients(raw);

      const edits = mcpClientUpdateEdits(raw, fs, { ...fs, description: '文件系统' });

      expect(edits).toEqual([{ path: ['clients', 'fs', 'description'], value: '文件系统' }]);
      expect(editYamlKeys(raw, edits).content).toBe(`${raw}    description: 文件系统\n`);
    });

    it('writes nothing for a new client unless the box is ticked', () => {
      const plain = mcpClientCreateEdits(CLIENTS, created('new'));
      const shared = mcpClientCreateEdits(CLIENTS, created('shared', { share_sessions: true }));

      expect(plain[0].value).not.toHaveProperty('share_sessions');
      expect(editYamlKeys(CLIENTS, shared).content).toBe(
        `${CLIENTS}  shared:\n    transport: streamable_http\n    enabled: false\n    share_sessions: true\n    description: 通过设置页面创建的 MCP Client。\n    url: ""\n    headers: {}\n`,
      );
    });
  });
});

describe('skill quick fields only touch the frontmatter keys they changed', () => {
  const SKILL = '---\nname: demo\n# 注释\nenabled: true\nstrategy: normal\nkeywords: ["a", "b"]\norder: 1\n---\n\n# Demo\n正文\n';

  it('reads the quick fields as the text the inputs show', () => {
    expect(parseSkillQuickFields(SKILL)).toEqual({
      enabled: true, strategy: 'normal', keywords: 'a, b', order: '1', priority: '0', scan_depth: '0',
    });
  });

  // 以前整段 frontmatter 重新 dump：注释没了，没动过的 priority / scan_depth 也被写成 0。
  it('rewrites just the changed keys and leaves comments and the body alone', () => {
    const fields = parseSkillQuickFields(SKILL);

    const result = serializeSkillQuickFields(SKILL, { ...fields, enabled: false, order: '3' });

    expect(result).toEqual({
      content: '---\nname: demo\n# 注释\nenabled: false\nstrategy: normal\nkeywords: ["a", "b"]\norder: 3\n---\n\n# Demo\n正文\n',
      preserved: true,
      note: '',
    });
  });

  it('keeps CRLF line endings', () => {
    const crlf = SKILL.replace(/\n/g, '\r\n');
    const fields = parseSkillQuickFields(crlf);

    const { content } = serializeSkillQuickFields(crlf, { ...fields, keywords: 'a, c' });

    expect(content).toBe(crlf.replace('keywords: ["a", "b"]', 'keywords: [a, c]'));
  });

  it('returns the text unchanged when nothing changed', () => {
    expect(serializeSkillQuickFields(SKILL, parseSkillQuickFields(SKILL)).content).toBe(SKILL);
  });

  it('refuses a number field that is not a number', () => {
    expect(() => serializeSkillQuickFields(SKILL, { ...parseSkillQuickFields(SKILL), priority: 'abc' })).toThrow('priority 必须是数字');
  });

  it('refuses a file without frontmatter', () => {
    expect(() => parseSkillQuickFields('# 没有 frontmatter\n')).toThrow('技能文件缺少 YAML frontmatter，请修复后再保存。');
  });
});

// 手写成字符串的开关要和引擎的 parse_bool 读出同一个结论，不然 "false" 在表单上显示的和引擎实际做的相反
describe('hand-written switches read like the engine reads them', () => {
  it.each([
    [true, false, true],
    [false, true, false],
    ['true', false, true],
    ['Yes', false, true],
    ['on', false, true],
    ['y', false, true],
    [1, false, true],
    ['false', true, false],
    ['No', true, false],
    ['off', true, false],
    ['n', true, false],
    [0, true, false],
    ['maybe', false, false],
    ['maybe', true, true],
    ['', true, true],
    [null, true, true],
    [undefined, false, false],
  ])('parseBoolFlag(%j, fallback %s) is %s', (value, fallback, expected) => {
    expect(parseBoolFlag(value, fallback)).toBe(expected);
  });

  it('reads persistent on a node', () => {
    expect(parseNodeConfig('id: a\npersistent: "false"\n').persistent).toBe(false);
    expect(parseNodeConfig('id: a\npersistent: "yes"\n').persistent).toBe(true);
    expect(parseNodeConfig('id: a\npersistent: true\n').persistent).toBe(true);
    expect(parseNodeConfig('id: a\n').persistent).toBe(false);
  });

  it('reads enabled, once and silent on a schedule', () => {
    const [quoted, bare] = parseSchedules([
      'schedules:',
      '  - id: quoted',
      '    cron: "0 8 * * *"',
      '    type: script',
      '    command: run.py',
      '    enabled: "false"',
      '    once: "yes"',
      '    silent: "off"',
      '  - id: bare',
      '    cron: "0 9 * * *"',
      '    text: 早',
      '',
    ].join('\n'));

    expect(quoted).toMatchObject({ enabled: false, once: true, silent: false });
    expect(bare).toMatchObject({ enabled: true, once: false, silent: true });
  });

  it('leaves a quoted switch alone when another field of the schedule is edited', () => {
    const raw = 'schedules:\n  - id: quoted\n    cron: "0 8 * * *"\n    text: 早\n    enabled: "false"\n';
    const [quoted] = parseSchedules(raw);

    const { content } = editYamlKeys(raw, scheduleUpdateEdits(raw, quoted, { ...quoted, text: '早安' }));

    expect(content).toBe(raw.replace('text: 早\n', 'text: 早安\n'));
  });

  it('reads enabled on an MCP client', () => {
    const [off, on] = parseMcpClients('clients:\n  off:\n    command: a\n    enabled: "no"\n  on:\n    command: b\n');

    expect(off.enabled).toBe(false);
    expect(on.enabled).toBe(true);
  });

  it('reads enabled on a skill', () => {
    expect(parseSkillQuickFields('---\nname: s\nenabled: "false"\n---\n正文\n').enabled).toBe(false);
    expect(parseSkillQuickFields('---\nname: s\n---\n正文\n').enabled).toBe(true);
  });
});
