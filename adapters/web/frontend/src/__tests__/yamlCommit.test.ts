// 结构化表单写回配置文件只走 editYamlKeys / commitYamlKeys 这一条：读最新一版，只改动过的键，带着那一版的指纹写回。
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../api/supervisorClient';
import { commitRuntimeKeys, commitYamlKeys, runtimeInstanceLayer, StaleKeysError, type YamlSource } from '../console/yamlCommit';
import { editYamlKeys, parseYamlMapping } from '../console/yamlRewrite';

vi.mock('../api/supervisorClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/supervisorClient')>()),
  getRuntimeFile: vi.fn(),
  updateRuntimeRaw: vi.fn(),
}));

// 贴着 defaults/nodes/qq.orchestrator.yaml 的形状：prompt 正文里混着长得像配置的行。
const NODE = [
  'id: qq.orchestrator',
  'type: ai',
  'name: "QQ 综合入口"',
  'delegate_targets:',
  '  - bootstrap.executor',
  '  - draw.image_gen',
  'tool_access:',
  '  mode: allowlist',
  '  allow:',
  '    - web_search',
  '    - read_web',
  'skills:',
  '  mode: none',
  'prompt: |',
  '  {{include:_persona.md}}',
  '',
  '  # Role: QQ Orchestrator',
  '  - 这一行以短横开头，但它是 prompt 正文',
  '  tool_access:',
  '    allow:',
  '      - 这是 prompt 里的假配置',
  '',
].join('\n');

describe('editYamlKeys', () => {
  it('appends to the real list and leaves the look-alike inside the prompt alone', () => {
    const { content, preserved } = editYamlKeys(NODE, [{ path: ['tool_access', 'allow'], value: ['web_search', 'read_web', 'qq_forward'] }]);

    expect(content).toBe(NODE.replace('    - read_web\n', '    - read_web\n    - qq_forward\n'));
    expect(preserved).toBe(true);
  });

  it('keeps CRLF on every line it writes', () => {
    const crlf = NODE.split('\n').join('\r\n');

    const { content } = editYamlKeys(crlf, [{ path: ['model'], value: 'gpt-4o' }]);

    expect(content).toBe(`${crlf}model: gpt-4o\r\n`);
  });

  it('builds the missing levels inside an existing block and keeps every comment', () => {
    const runtime = ['# 顶部注释', 'engine:', '  tool_mode: json                   # 行尾注释', '', 'providers:', '  openai:',
      '    timeout_sec: 600.0', '', '# 尾部注释', 'routing:', '  vision:', ''].join('\n');

    const { content } = editYamlKeys(runtime, [{ path: ['providers', 'openai', 'options', 'reasoning_effort'], value: 'high' }]);

    expect(content).toBe(runtime.replace('    timeout_sec: 600.0\n', '    timeout_sec: 600.0\n    options:\n      reasoning_effort: high\n'));
  });

  it('fills an options: {} placeholder and keeps the comment on that line', () => {
    const { content } = editYamlKeys('providers:\n  openai:\n    options: {}   # 先占位\n', [{ path: ['providers', 'openai', 'options', 'temperature'], value: 0.5 }]);

    expect(content).toBe('providers:\n  openai:\n    options: {temperature: 0.5}   # 先占位\n');
    expect(parseYamlMapping(content)).toEqual({ providers: { openai: { options: { temperature: 0.5 } } } });
  });

  it('refuses to write under a parent that already holds a value', () => {
    expect(() => editYamlKeys('providers:\n  openai: fast\n', [{ path: ['providers', 'openai', 'options', 'x'], value: 1 }]))
      .toThrow('配置路径 openai 不是映射，原文件未修改');
  });

  it('drops a key and the parents it leaves empty', () => {
    const { content } = editYamlKeys('engine:\n  compact:\n    keep_recent: 6\nshell:\n  x: 1\n', [{ path: ['engine', 'compact', 'keep_recent'], value: undefined }]);

    expect(content).toBe('shell:\n  x: 1\n');
  });

  describe('against the real qq.orchestrator.yaml', () => {
    // 手写的样例再像也只是样例。真文件格式一变，这里先红。
    const real = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../../../../defaults/nodes/qq.orchestrator.yaml'), 'utf-8');
    const doc = parseYamlMapping(real) as { tool_access: { allow: string[] }; delegate_targets: string[] };

    it('writing the same values back gives the identical file', () => {
      const { content } = editYamlKeys(real, [
        { path: ['tool_access', 'allow'], value: doc.tool_access.allow },
        { path: ['delegate_targets'], value: doc.delegate_targets },
      ]);

      expect(content).toBe(real);
    });

    it('dropping one tool removes that line and nothing else', () => {
      expect(doc.tool_access.allow).toContain('read_web');

      const { content } = editYamlKeys(real, [{ path: ['tool_access', 'allow'], value: doc.tool_access.allow.filter((name) => name !== 'read_web') }]);

      expect(content.split('\n')).toHaveLength(real.split('\n').length - 1);
      expect(content.slice(content.indexOf('prompt:'))).toBe(real.slice(real.indexOf('prompt:')));
    });
  });
});

interface FakeFile extends YamlSource {
  writes: Array<[string, string | null]>;
}

function fakeFile(content: string, hash: string | null = 'h1'): FakeFile & { read: () => Promise<YamlSource>; write: (next: string, base: string | null) => Promise<{ ok: boolean; hash: string }> } {
  const file = {
    content,
    hash,
    writes: [] as Array<[string, string | null]>,
    read: async () => ({ content: file.content, hash: file.hash }),
    write: async (next: string, base: string | null) => {
      file.writes.push([next, base]);
      file.content = next;
      file.hash = 'h2';
      return { ok: true, hash: 'h2' };
    },
  };
  return file;
}

describe('commitYamlKeys', () => {
  it('applies the edits to the latest text and writes with its fingerprint', async () => {
    const file = fakeFile('a: 1   # 注释\nb: 2\n');

    const saved = await commitYamlKeys({ read: file.read, write: file.write, edits: [{ path: ['b'], value: 3 }] });

    expect(file.writes).toEqual([['a: 1   # 注释\nb: 3\n', 'h1']]);
    expect(saved).toEqual({ content: 'a: 1   # 注释\nb: 3\n', note: '', result: { ok: true, hash: 'h2' }, receiptProblem: '' });
  });

  // 写已经落盘了：当成失败，表单会说没存上，人再点一次；每次写回都先读最新一版，下一次写不靠这份回执。
  it('treats an unreadable receipt as written and passes the reason on', async () => {
    const file = fakeFile('b: 2\n');
    const write = async (next: string, base: string | null) => {
      await file.write(next, base);
      throw new api.ReceiptUnreadable('节点 n 已经存上，可回执格式不对：缺 hash');
    };

    const saved = await commitYamlKeys({ read: file.read, write, edits: [{ path: ['b'], value: 3 }] });

    expect(saved).toEqual({ content: 'b: 3\n', note: '', result: null, receiptProblem: '节点 n 已经存上，可回执格式不对：缺 hash' });
    expect(file.content).toBe('b: 3\n');
  });

  it('still fails when the write itself was refused', async () => {
    const file = fakeFile('b: 2\n');
    const write = async () => { throw new api.ApiError(409, 'x 已被别处改过，刷新后再改'); };

    await expect(commitYamlKeys({ read: file.read, write, edits: [{ path: ['b'], value: 3 }] }))
      .rejects.toMatchObject({ status: 409 });
  });

  it('keeps what someone else changed in another key since the form opened', async () => {
    const file = fakeFile('a: 9\nb: 2\n');

    await commitYamlKeys({ read: file.read, write: file.write, edits: [{ path: ['b'], value: 3 }], loaded: 'a: 1\nb: 2\n' });

    expect(file.content).toBe('a: 9\nb: 3\n');
  });

  it('refuses to overwrite a key that changed since the form opened', async () => {
    const file = fakeFile('a: 1\nb: 5\n');

    const saving = commitYamlKeys({ read: file.read, write: file.write, edits: [{ path: ['b'], value: 3 }], loaded: 'a: 1\nb: 2\n' });

    await expect(saving).rejects.toBeInstanceOf(StaleKeysError);
    await expect(saving).rejects.toMatchObject({ status: 409, paths: ['b'] });
    expect(file.writes).toEqual([]);
  });

  it('will not check key by key without a fingerprint to write against', async () => {
    const file = fakeFile('b: 2\n', null);

    await expect(commitYamlKeys({ read: file.read, write: file.write, edits: [{ path: ['b'], value: 3 }], loaded: 'b: 2\n' }))
      .rejects.toThrow('服务端未返回配置版本，无法安全保存，请更新服务后重试。');
    expect(file.writes).toEqual([]);
  });

  it('stops before writing when the caller no longer wants the result', async () => {
    const file = fakeFile('b: 2\n');

    await expect(commitYamlKeys({ read: file.read, write: file.write, edits: [{ path: ['b'], value: 3 }], proceed: () => false }))
      .rejects.toMatchObject({ name: 'AbortError' });
    expect(file.writes).toEqual([]);
  });

  it('works out list edits from the latest text it read', async () => {
    const file = fakeFile('items:\n  - one\n');
    const seen: string[] = [];

    await commitYamlKeys({
      read: file.read,
      write: file.write,
      edits: (base) => {
        seen.push(base);
        const items = (parseYamlMapping(base).items as string[]) ?? [];
        return [{ path: ['items'], value: [...items, 'two'] }];
      },
    });

    expect(seen).toEqual(['items:\n  - one\n']);
    expect(file.content).toBe('items:\n  - one\n  - two\n');
  });

  it('refuses a broken original instead of merging onto it', async () => {
    const file = fakeFile('a: [\n');

    await expect(commitYamlKeys({ read: file.read, write: file.write, edits: [{ path: ['a'], value: 1 }] }))
      .rejects.toThrow('原文不是合法的 YAML');
    expect(file.writes).toEqual([]);
  });
});

describe('commitRuntimeKeys', () => {
  const DEFAULTS = '# 仓库默认\nengine:\n  max_workers: 4\n  max_steps: 64\n';

  beforeEach(() => {
    vi.mocked(api.getRuntimeFile).mockReset();
    vi.mocked(api.updateRuntimeRaw).mockReset().mockResolvedValue({ ok: true, hash: 'r2' });
  });

  it('starts from an empty instance layer while the repository default is in use', async () => {
    vi.mocked(api.getRuntimeFile).mockResolvedValue({ content: DEFAULTS, hash: 'd1', source: 'default', path: 'defaults/runtime.yaml' });

    const { content } = await commitRuntimeKeys('token', [{ path: ['engine', 'max_workers'], value: 8 }]);

    // 写的只有改的那一个键：默认全文抄进实例层，仓库以后再改默认值这个实例就拿不到了。
    expect(content).toBe('engine:\n  max_workers: 8\n');
    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('token', 'engine:\n  max_workers: 8\n', 'd1');
  });

  it('edits the instance layer in place once it exists', async () => {
    vi.mocked(api.getRuntimeFile).mockResolvedValue({ content: 'engine:\n  max_workers: 2  # 本实例\n', hash: 'o1', source: 'override', path: 'config/runtime.yaml' });

    await commitRuntimeKeys('token', [{ path: ['engine', 'max_workers'], value: 8 }]);

    expect(api.updateRuntimeRaw).toHaveBeenCalledWith('token', 'engine:\n  max_workers: 8  # 本实例\n', 'o1');
  });

  it('checks staleness against what the page showed, not the empty layer it writes onto', async () => {
    vi.mocked(api.getRuntimeFile).mockResolvedValue({ content: DEFAULTS.replace('max_workers: 4', 'max_workers: 6'), hash: 'd2', source: 'default', path: 'defaults/runtime.yaml' });

    await expect(commitRuntimeKeys('token', [{ path: ['engine', 'max_workers'], value: 8 }], { loaded: DEFAULTS }))
      .rejects.toBeInstanceOf(StaleKeysError);
    expect(api.updateRuntimeRaw).not.toHaveBeenCalled();
  });

  it('names the instance layer of each source', () => {
    expect(runtimeInstanceLayer({ content: DEFAULTS, hash: 'd1', source: 'default', path: 'defaults/runtime.yaml' })).toBe('');
    expect(runtimeInstanceLayer({ content: 'a: 1\n', hash: 'o1', source: 'override', path: 'config/runtime.yaml' })).toBe('a: 1\n');
  });
});
