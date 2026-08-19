// [2026-06-02] Structured Settings parser tests.
// Why: Settings forms serialize operational YAML without a frontend YAML dependency.
// How: cover providers, schedules, MCP clients, and skill frontmatter round trips.
// Purpose: future UI edits do not silently corrupt the common config shapes.
import { describe, expect, it } from 'vitest';

import type { RuntimeConfigFormState } from '../components/settings/settingsStructuredConfig';

import {
  parseMcpClients,
  parseNodeConfig,
  parseProvidersFromRuntime,
  parseRuntimeConfig,
  parseSchedules,
  parseSkillMarkdown,
  replaceProvidersInRuntime,
  serializeMcpClients,
  serializeNodeConfig,
  serializeRuntimeConfig,
  serializeSchedules,
  serializeSkillMarkdown,
} from '../components/settings/settingsStructuredConfig';

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
    const saved = serializeNodeConfig(ALLOWLIST_NODE, parseNodeConfig(ALLOWLIST_NODE));

    expect(saved).toContain('mode: allowlist');
    expect(saved).toContain('- web_search');
    expect(saved).toContain('- qq_forward');
  });

  it('keeps deny under mode all instead of dropping it', () => {
    const raw = ['id: bootstrap.executor', 'tool_access:', '  mode: all',
      '  deny:', '    - execute_command', ''].join('\n');

    const saved = serializeNodeConfig(raw, parseNodeConfig(raw));

    expect(saved).toContain('mode: all');
    expect(saved).toContain('- execute_command');
  });

  it('still writes the fields it does own', () => {
    const saved = serializeNodeConfig(ALLOWLIST_NODE, {
      ...parseNodeConfig(ALLOWLIST_NODE),
      name: '改过的名字',
    });

    expect(saved).toContain('改过的名字');
    expect(saved).toContain('mode: allowlist');
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
    tool_mode: 'fake-native',
    max_workers: '',
    compact_threshold_tokens: '',
    compact_hard_threshold_tokens: '',
    compact_keep_recent_tokens: '',
    compact_keep_recent: '',
    ...over,
  });

  it('reads the nested keys the engine actually consumes', () => {
    const form = parseRuntimeConfig(RUNTIME);

    expect(form.entry_node_id).toBe('bootstrap.shell_orchestrator');
    expect(form.tool_mode).toBe('json');
    expect(form.max_workers).toBe('4');
  });

  it('writes back into the nested keys without inventing top-level ones', () => {
    const saved = serializeRuntimeConfig(RUNTIME, form({
      entry_node_id: 'qq.orchestrator', tool_mode: 'native', max_workers: '8',
    }));

    expect(saved).toContain('  entry_node_id: qq.orchestrator');
    expect(saved).toContain('  tool_mode: native');
    expect(saved).toContain('  max_workers: 8');
    expect(saved).not.toMatch(/^entry_node_id:/m);
    expect(saved).not.toMatch(/^tool_mode:/m);
    expect(saved).not.toMatch(/^max_concurrent_tasks:/m);
  });

  it('refuses to serialize onto an empty base', () => {
    // 空基底会让整份 runtime.yaml 被这三个键替换掉：163 行的文件剩三行，
    // providers / routing / meta / tools / skills / memory / maintenance 全没了。
    expect(() => serializeRuntimeConfig('', form())).toThrow();
    expect(() => serializeRuntimeConfig('    ', form())).toThrow();
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

    const saved = serializeRuntimeConfig(full, form({
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
    const saved = serializeRuntimeConfig(RUNTIME, form({ entry_node_id: 'x', tool_mode: 'json' }));

    expect(saved).not.toContain('max_workers');
  });

  it('creates the parent block when runtime.yaml lacks it', () => {
    const saved = serializeRuntimeConfig('version: 1\n', form({
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
      const saved = serializeRuntimeConfig(COMMENTED, form({
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
      const saved = serializeRuntimeConfig(COMMENTED, form({
        entry_node_id: 'bootstrap.shell_orchestrator',
        compact_threshold_tokens: '120000',
      }));

      expect(saved).toContain('# Clonoth runtime tuning config');
      expect(saved).toContain('max_steps: 64            # 单轮最多几步');
      expect(saved).toContain('threshold_tokens: 120000   # 软阈值：超过此值在后台静默压缩');
    });

    it('keeps 0 rather than treating it as unset', () => {
      const saved = serializeRuntimeConfig(COMMENTED, form({
        entry_node_id: 'bootstrap.shell_orchestrator',
        compact_threshold_tokens: '0',
      }));

      expect(parseRuntimeConfig(saved).compact_threshold_tokens).toBe('0');
    });

    it('builds the compact block when runtime.yaml has none', () => {
      const saved = serializeRuntimeConfig('engine:\n  max_steps: 64\n', form({
        compact_threshold_tokens: '90000',
      }));

      expect(parseRuntimeConfig(saved).compact_threshold_tokens).toBe('90000');
      expect(saved).toContain('max_steps: 64');
    });

    it('drops the key when the field is cleared', () => {
      const saved = serializeRuntimeConfig(COMMENTED, form({
        entry_node_id: 'bootstrap.shell_orchestrator',
      }));

      expect(saved).not.toContain('threshold_tokens');
      expect(saved).toContain('# Clonoth runtime tuning config');
    });
  });
});

describe('settingsStructuredConfig', () => {
  it('replaces only the runtime providers block', () => {
    const raw = 'version: 1\nengine:\n  model: ""\nproviders:\n  openai:\n    timeout_sec: 600.0\nmeta:\n  x: 1\n';
    const parsed = parseProvidersFromRuntime(raw);
    expect(parsed.providers.openai.timeout_sec).toBe(600);

    const next = replaceProvidersInRuntime(raw, { openai: { timeout_sec: 120, base_url: 'https://example.test/v1' } });
    expect(next).toContain('providers:\n  openai:\n    timeout_sec: 120\n    base_url: https://example.test/v1');
    expect(next).toContain('engine:\n  model: ""');
    expect(next).toContain('meta:\n  x: 1');
  });

  it('round trips editable schedules', () => {
    const raw = 'schedules:\n- id: demo\n  cron: "*/5 * * * *"\n  type: script\n  command: python task.py\n  enabled: false\n  once: true\n  text: "运行脚本"\n';
    const schedules = parseSchedules(raw);
    expect(schedules).toHaveLength(1);
    expect(schedules[0].id).toBe('demo');
    expect(schedules[0].type).toBe('script');
    expect(schedules[0].enabled).toBe(false);

    schedules[0].enabled = true;
    const serialized = serializeSchedules(schedules);
    expect(serialized).toContain('- id: demo');
    expect(serialized).toContain('type: script');
    expect(serialized).toContain('enabled: true');
  });

  it('round trips MCP clients with stdio args and HTTP headers', () => {
    const raw = 'version: 1\nclients:\n  local:\n    transport: stdio\n    enabled: true\n    command: npx\n    args:\n    - -y\n    - server\n  remote:\n    transport: streamable_http\n    enabled: false\n    url: https://mcp.example.test\n    headers:\n      Authorization: Bearer token\n';
    const clients = parseMcpClients(raw);
    expect(clients.map((client) => client.id)).toEqual(['local', 'remote']);
    expect(clients[0].argsText).toContain('server');
    expect(clients[1].headersText).toContain('Authorization: Bearer token');

    const serialized = serializeMcpClients(clients);
    expect(serialized).toContain('local:');
    expect(serialized).toContain('transport: stdio');
    expect(serialized).toContain('remote:');
    expect(serialized).toContain('transport: streamable_http');
  });

  it('round trips skill frontmatter and body', () => {
    const raw = '---\nname: demo\ndescription: 技能\nenabled: true\nstrategy: normal\nkeywords: ["a", "b"]\norder: 1\npriority: 2\nscan_depth: 3\n---\n\n# Demo\n正文\n';
    const form = parseSkillMarkdown(raw, 'fallback');
    expect(form.name).toBe('demo');
    expect(form.keywordsText).toBe('a\nb');
    form.enabled = false;
    form.body = '# Demo\n更新正文\n';

    const serialized = serializeSkillMarkdown(form);
    expect(serialized).toContain('enabled: false');
    expect(serialized).toContain('keywords:\n  - a\n  - b');
    expect(serialized).toContain('# Demo\n更新正文');
  });

  // 这几份 yaml 都由 PyYAML（YAML 1.1）读回：裸写的 on/off/yes/no 变成布尔，12:30 变成 750。
  // 仓库里两个序列化器共用同一份危险，别再让它们分头决定要不要 noCompatMode。
  it('quotes the values a YAML 1.1 reader would fold', () => {
    const clients = parseMcpClients('version: 1\nclients:\n  local:\n    transport: stdio\n    command: npx\n');
    clients[0].envText = 'STRICT: no\nMODE: on\nWINDOW: 12:30\nNAME: 早安';

    const serialized = serializeMcpClients(clients);

    expect(serialized).toContain('STRICT: "no"');
    expect(serialized).toContain('MODE: "on"');
    expect(serialized).toContain('WINDOW: "12:30"');
    // 安全的值不该被无谓加引号，否则每次保存的 diff 全是噪音。
    expect(serialized).toContain('NAME: 早安');
  });

  it('quotes a schedule field that reads as a boolean', () => {
    const schedules = parseSchedules('schedules:\n- id: demo\n  cron: "*/5 * * * *"\n  type: message\n  text: hi\n');
    schedules[0].text = 'off';
    schedules[0].conversation_key = 'y';

    const serialized = serializeSchedules(schedules);

    expect(serialized).toContain('text: "off"');
    expect(serialized).toContain('conversation_key: "y"');
  });
});
