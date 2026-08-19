// 节点授权：哪个节点能调哪些工具。allowlist 勾中的是允许，all 勾中的是禁止，两者相反。
import { useEffect, useMemo, useState } from 'react';

import {
  getAllToolNames,
  getNodeRaw,
  getNodes,
  updateNodeRaw,
  type AdminNode,
} from '../../../api/supervisorClient';
import {
  NodeYamlShapeError,
  upsertYamlList,
  upsertYamlNested,
} from '../../../console/nodeYaml';
import { useSettingsStore } from '../../../store/settingsStore';
import { inferToolRisk, riskClassName, riskLabel } from '../../../utils/toolRisk';
import { Button } from '../../common';
import { Card, StatusText, TextInput } from './settingsPagePrimitives';

type Mode = 'none' | 'all' | 'allowlist';

interface Access {
  mode: Mode;
  allow: string[];
  deny: string[];
}

const MODES: Array<{ value: Mode; label: string; picking: string }> = [
  { value: 'allowlist', label: '白名单', picking: '勾中的可以调用，其余一律看不到' },
  { value: 'all', label: '全部放开', picking: '勾中的被禁止，其余全部可以调用' },
  { value: 'none', label: '不给工具', picking: '这个节点拿不到任何工具' },
];

const asMode = (value: unknown): Mode => {
  const text = String(value || 'none').trim().toLowerCase();
  return text === 'all' || text === 'allowlist' ? text : 'none';
};

const readAccess = (node: AdminNode): Access => {
  const raw = node.tool_access;
  // 节点 yaml 允许把 tool_access 直接写成 "all" 这种字符串简写。
  if (typeof raw === 'string') return { mode: asMode(raw), allow: [], deny: [] };
  const ta = (raw || {}) as Record<string, unknown>;
  const list = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((item) => typeof item === 'string') : [];
  return { mode: asMode(ta.mode), allow: list(ta.allow), deny: list(ta.deny) };
};

const sameList = (a: readonly string[], b: readonly string[]): boolean =>
  a.length === b.length && a.every((item, index) => item === b[index]);

const NodeRow = ({
  node, tools, onSaved,
}: { node: AdminNode; tools: string[]; onSaved: () => void }) => {
  const { adminToken } = useSettingsStore();
  const saved = useMemo(() => readAccess(node), [node]);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>(saved.mode);
  const [allow, setAllow] = useState<string[]>(saved.allow);
  const [deny, setDeny] = useState<string[]>(saved.deny);
  const [filter, setFilter] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    setMode(saved.mode);
    setAllow(saved.allow);
    setDeny(saved.deny);
  }, [saved]);

  const picked = mode === 'all' ? deny : allow;
  const setPicked = mode === 'all' ? setDeny : setAllow;
  const dirty = mode !== saved.mode || !sameList(allow, saved.allow) || !sameList(deny, saved.deny);

  // 已经配过但当前注册表里没有的名字要留着：插件没加载时把它清掉，就再也勾不回来了。
  const candidates = useMemo(() => {
    const merged = [...new Set([...tools, ...saved.allow, ...saved.deny])].sort();
    const keyword = filter.trim().toLowerCase();
    return keyword ? merged.filter((name) => name.toLowerCase().includes(keyword)) : merged;
  }, [tools, saved, filter]);

  const toggle = (name: string) => {
    setPicked(picked.includes(name) ? picked.filter((item) => item !== name) : [...picked, name].sort());
  };

  const save = async () => {
    if (!adminToken) return;
    setBusy(true);
    setMessage('');
    try {
      const raw = await getNodeRaw(adminToken, node.id);
      let next = upsertYamlNested(raw, ['tool_access', 'mode'], mode);
      if (mode === 'allowlist') next = upsertYamlList(next, ['tool_access', 'allow'], allow);
      if (mode === 'all') next = upsertYamlList(next, ['tool_access', 'deny'], deny);
      await updateNodeRaw(adminToken, node.id, next);
      setMessage('已保存，下一个任务即生效');
      onSaved();
    } catch (error) {
      setMessage(
        error instanceof NodeYamlShapeError
          ? `${error.message}（请到「节点文件」页改原文）`
          : error instanceof Error ? error.message : '保存失败',
      );
    }
    setBusy(false);
  };

  const summary = saved.mode === 'allowlist'
    ? `${saved.allow.length} 个工具`
    : saved.mode === 'all'
      ? saved.deny.length ? `全部，禁用 ${saved.deny.length} 个` : '全部'
      : '无';
  const modeMeta = MODES.find((item) => item.value === mode);

  return (
    <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)]">
      <button
        className="flex w-full flex-wrap items-center gap-2 p-3 text-left"
        onClick={() => setOpen((prev) => !prev)}
        type="button"
      >
        <span className="font-mono text-xs font-semibold text-[var(--duties-text)]">{node.id}</span>
        <span className="border border-[var(--duties-border)] px-1.5 py-0.5 font-mono text-[0.55rem] text-[var(--duties-secondary)]">
          {MODES.find((item) => item.value === saved.mode)?.label}
        </span>
        <span className="text-[0.65rem] text-[var(--duties-tertiary)]">{summary}</span>
        <span className="ml-auto font-mono text-[0.65rem] text-[var(--duties-tertiary)]">{open ? '收起' : '展开'}</span>
      </button>

      {open && (
        <div className="border-t border-[var(--duties-border)] p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            {MODES.map((item) => (
              <button
                className={`border px-2 py-1 font-mono text-[0.65rem] transition-colors ${
                  mode === item.value
                    ? 'border-[var(--duties-text)] bg-[var(--duties-text)] text-[var(--duties-bg)]'
                    : 'border-[var(--duties-border)] text-[var(--duties-secondary)] hover:border-[var(--duties-text)]'
                }`}
                key={item.value}
                onClick={() => setMode(item.value)}
                type="button"
              >
                {item.label}
              </button>
            ))}
            <span className="text-[0.6rem] text-[var(--duties-tertiary)]">{modeMeta?.picking}</span>
          </div>

          {mode !== 'none' && (
            <>
              <div className="mb-2 flex items-center gap-2">
                <TextInput
                  onChange={(event) => setFilter(event.target.value)}
                  placeholder="筛选工具名"
                  value={filter}
                />
                <Button onClick={() => setPicked(candidates)}>全选</Button>
                <Button onClick={() => setPicked([])}>清空</Button>
              </div>
              <div className="grid max-h-64 grid-cols-1 gap-1 overflow-y-auto sm:grid-cols-2">
                {candidates.map((name) => {
                  const risk = inferToolRisk(name);
                  const missing = !tools.includes(name);
                  return (
                    <label
                      className="flex items-center gap-2 border border-[var(--duties-border)] px-2 py-1"
                      key={name}
                    >
                      <input
                        checked={picked.includes(name)}
                        onChange={() => toggle(name)}
                        type="checkbox"
                      />
                      <span className="min-w-0 flex-1 truncate font-mono text-[0.65rem] text-[var(--duties-text)]">
                        {name}
                      </span>
                      {missing && (
                        <span className="font-mono text-[0.55rem] text-[var(--duties-tertiary)]" title="当前注册表里没有这个工具">
                          未注册
                        </span>
                      )}
                      <span className={`border px-1 py-0.5 font-mono text-[0.5rem] ${riskClassName(risk)}`}>
                        {riskLabel(risk)}
                      </span>
                    </label>
                  );
                })}
              </div>
            </>
          )}

          <div className="mt-3 flex items-center gap-2 border-t border-[var(--duties-border)] pt-3">
            <span className="min-w-0 flex-1 truncate text-[0.65rem] text-[var(--duties-tertiary)]">
              {message || (dirty ? '有未保存的改动' : '与服务器一致')}
            </span>
            <Button disabled={!dirty || busy} onClick={() => { setMode(saved.mode); setAllow(saved.allow); setDeny(saved.deny); setMessage(''); }}>
              还原
            </Button>
            <Button disabled={!dirty || busy} onClick={() => void save()} variant="primary">
              {busy ? '保存中' : '保存'}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
};

export const NodeGrantsSection = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [nodes, setNodes] = useState<AdminNode[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [message, setMessage] = useState('');

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    try {
      const [nodeList, toolNames] = await Promise.all([
        getNodes(adminToken),
        getAllToolNames(adminToken),
      ]);
      setNodes(nodeList);
      setTools(toolNames);
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '读取节点失败');
    }
  };

  useEffect(() => { void load(); }, [adminToken, isAuthenticated]);

  return (
    <Card
      description="每个节点各自决定能看到哪些工具。没授权的工具不会出现在给模型的清单里，模型不知道它存在。"
      title="节点授权"
    >
      <div className="space-y-2">
        {nodes.map((node) => (
          <NodeRow key={node.id} node={node} onSaved={() => void load()} tools={tools} />
        ))}
        {nodes.length === 0 && (
          <p className="text-xs text-[var(--duties-tertiary)]">{message || '正在读取…'}</p>
        )}
      </div>
      <StatusText message={nodes.length ? message : ''} />
    </Card>
  );
};
