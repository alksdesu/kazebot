// 节点授权：这个节点自己能调哪些工具。allowlist 勾中的是允许，all 勾中的是禁止，两者相反。
import { useEffect, useMemo, useState, type ReactNode } from 'react';

import {
  getAllToolNames,
  getEffectiveTools,
  getNodeRaw,
  getNodes,
  type AdminNode,
  type EffectiveTool,
  updateNodeRaw,
} from '../../../api/supervisorClient';
import {
  NodeYamlShapeError,
  upsertYamlList,
  upsertYamlNested,
} from '../../../console/nodeYaml';
import { useSettingsStore } from '../../../store/settingsStore';
import { inferToolRisk, riskLabel } from '../../../utils/toolRisk';
import {
  Button,
  Check,
  Empty,
  ErrorText,
  Facts,
  Footnote,
  Input,
  Item,
  ItemTitle,
  List,
  SaveBar,
  Segmented,
  Tag,
} from './settingsControls';
import { Card, StatusText } from './settingsPagePrimitives';

type Mode = 'none' | 'all' | 'allowlist';

interface Access {
  mode: Mode;
  allow: string[];
  deny: string[];
}

const MODE_CHOICES: ReadonlyArray<readonly [Mode, string]> = [
  ['allowlist', '白名单'],
  ['all', '全部放开'],
  ['none', '不给工具'],
];

const MODE_LABEL = Object.fromEntries(MODE_CHOICES) as Record<Mode, string>;

const MODE_HINT: Record<Mode, string> = {
  allowlist: '勾中的可以调用，其余一律看不到',
  all: '勾中的被禁止，其余全部可以调用',
  none: '这个节点拿不到任何工具',
};

// 落到机器、密钥和新智能体上的那几个，配得宽不宽先看它们在不在里面。
const NOTABLE = ['execute_command', 'remote_exec', 'manage_secret', 'create_agent'];

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

const describeGrant = (access: Access): string => {
  if (access.mode === 'allowlist') return `${access.allow.length} 个工具`;
  if (access.mode === 'all') return access.deny.length ? `全部工具，禁用 ${access.deny.length} 个` : '全部工具';
  return '无';
};

const notableIn = (access: Access): string[] => {
  if (access.mode === 'allowlist') return NOTABLE.filter((name) => access.allow.includes(name));
  if (access.mode === 'all') return NOTABLE.filter((name) => !access.deny.includes(name));
  return [];
};

// settingsControls 的 Tag 只有 neutral/live 两档，也不收 title，工具行的角标在这里单画。
const MARK_TONE = {
  danger: 'border-[var(--duties-danger)] text-[var(--duties-danger)]',
  muted: 'border-[var(--duties-border)] text-[var(--duties-secondary)]',
} as const;

const Mark = ({ children, title, tone }: {
  children: ReactNode;
  title: string;
  tone: keyof typeof MARK_TONE;
}) => (
  <span
    className={`flex-none border px-1.5 py-0.5 font-mono text-[0.6rem] ${MARK_TONE[tone]}`}
    title={title}
  >
    {children}
  </span>
);

const DefaultsNotice = ({ nodes }: { nodes: AdminNode[] }) => {
  const wide = useMemo(() => nodes
    .map((node) => ({ id: node.id, access: readAccess(node) }))
    .map((row) => ({ ...row, notable: notableIn(row.access) }))
    .filter((row) => row.notable.length > 0)
    .sort((a, b) => b.notable.length - a.notable.length), [nodes]);

  return (
    <div className="mb-4 border border-[var(--duties-danger)] bg-[var(--duties-bg)] px-4 py-3">
      <h3 className="m-0 text-sm font-semibold text-[var(--duties-danger)]">这里的默认值是「全放开」，不是「全收紧」</h3>
      <p className="mt-1.5 text-xs leading-5 text-[var(--duties-secondary)]">
        在「智能体」页新建 AI 节点，模板写的是 <code>tool_access.mode: all</code>：一建出来就握着当前
        注册的每一个工具，以后新装的工具也自动算它的。要收紧得自己来，没人会替你收。
      </p>
      {wide.length > 0 && (
        <ul className="m-0 mt-2 list-none p-0">
          {wide.map((row) => (
            <li className="text-xs leading-5 text-[var(--duties-secondary)]" key={row.id}>
              <code>{row.id}</code> 现在拿着{describeGrant(row.access)}，含 {row.notable.join('、')}
              ——执行命令、远程执行、读写密钥、再造新智能体都在里面。
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};

const EffectiveNotes = ({ rows }: { rows: EffectiveTool[] }) => {
  const dead = rows.filter((row) => !row.registered).map((row) => row.name);
  const gated = rows.filter((row) => row.gated).map((row) => row.name);
  const loose = rows.filter((row) => row.guarded === false).length;
  if (!dead.length && !gated.length && !loose) return null;
  return (
    <div className="mb-2">
      {!!dead.length && <ErrorText>这些名字不存在，注入时会被静默忽略：{dead.join('、')}</ErrorText>}
      {!!gated.length && <ErrorText>配了但用不了，渠道没设 model：{gated.join('、')}</ErrorText>}
      {!!loose && <Facts>其中 {loose} 个是外部脚本，没声明 guard，「服务端策略」那一页管不到它们。</Facts>}
    </div>
  );
};

const ToolCell = ({ effective, missing, name, picked, onToggle }: {
  effective?: EffectiveTool;
  missing: boolean;
  name: string;
  picked: boolean;
  onToggle: () => void;
}) => {
  const risk = inferToolRisk(name);
  return (
    <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-2 py-1">
      <Check checked={picked} onChange={onToggle}>
        <span className="flex flex-wrap items-center gap-1.5">
          <span className="break-all font-mono text-[0.7rem] text-[var(--duties-text)]">{name}</span>
          {missing && (
            <Mark title="当前注册表里没有这个工具，注入时会被静默忽略" tone="danger">未注册</Mark>
          )}
          {!!effective?.gated && <Mark title={effective.gated} tone="danger">不可用</Mark>}
          {effective?.guarded === false && (
            <Mark title="外部脚本没声明 guard，服务端策略那一页对它不生效" tone="muted">策略外</Mark>
          )}
          <Mark title="按工具名前缀推断" tone={risk === 'high' ? 'danger' : 'muted'}>{riskLabel(risk)}</Mark>
        </span>
      </Check>
    </div>
  );
};

const NodeRow = ({ node, tools, onSaved }: {
  node: AdminNode;
  tools: string[];
  onSaved: () => void;
}) => {
  const { adminToken } = useSettingsStore();
  const saved = useMemo(() => readAccess(node), [node]);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>(saved.mode);
  const [allow, setAllow] = useState<string[]>(saved.allow);
  const [deny, setDeny] = useState<string[]>(saved.deny);
  const [filter, setFilter] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [effective, setEffective] = useState<Record<string, EffectiveTool>>({});

  useEffect(() => {
    setMode(saved.mode);
    setAllow(saved.allow);
    setDeny(saved.deny);
  }, [saved]);

  // 勾了不等于能用，展开时才去问，避免为折叠着的节点白跑一趟。
  useEffect(() => {
    if (!open || !adminToken) return;
    let alive = true;
    void getEffectiveTools(adminToken, node.id)
      .then((data) => {
        if (!alive) return;
        setEffective(Object.fromEntries(data.tools.map((item) => [item.name, item])));
      })
      .catch(() => undefined);
    return () => { alive = false; };
  }, [open, adminToken, node.id, saved]);

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
    setPicked(picked.includes(name)
      ? picked.filter((item) => item !== name)
      : [...picked, name].sort());
  };

  const save = async () => {
    if (!adminToken) return;
    // 白名单里写错的名字注入时是静默忽略的，配了半天不生效也没有任何提示。
    // 不硬拦：插件没加载时注册表会短一截，拦死就没法保存了。
    const dead = mode === 'allowlist' ? allow.filter((name) => !tools.includes(name)) : [];
    if (dead.length
      && !window.confirm(`这些名字不在注册表里，保存后会被静默忽略：\n${dead.join('、')}\n\n仍要保存？`)) {
      return;
    }
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

  const delegates = node.delegate_targets?.length || 0;

  return (
    <Item>
      <button
        aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-2 text-left"
        onClick={() => setOpen((prev) => !prev)}
        type="button"
      >
        <ItemTitle className="font-mono">{node.id}</ItemTitle>
        <Tag>{MODE_LABEL[saved.mode]}</Tag>
        <span className="text-xs text-[var(--duties-secondary)]">{describeGrant(saved)}</span>
        {delegates > 0 && (
          <span className="text-xs text-[var(--duties-tertiary)]">可派给 {delegates} 个下游节点</span>
        )}
        <span className="flex-1" />
        <span className="font-mono text-xs text-[var(--duties-tertiary)]">{open ? '收起' : '展开'}</span>
      </button>

      {open && (
        <div className="mt-3 border-t border-[var(--duties-border)] pt-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <Segmented choices={MODE_CHOICES} onPick={setMode} value={mode} />
            <span className="text-xs text-[var(--duties-secondary)]">{MODE_HINT[mode]}</span>
          </div>

          {mode !== 'none' && (
            <>
              <div className="mb-2 flex items-center gap-2">
                <Input
                  aria-label={`筛选 ${node.id} 的工具名`}
                  onChange={(event) => setFilter(event.target.value)}
                  placeholder="筛选工具名"
                  value={filter}
                  width="flex"
                />
                <Button onClick={() => setPicked(candidates)} size="sm" tone="quiet">全选</Button>
                <Button onClick={() => setPicked([])} size="sm" tone="quiet">清空</Button>
              </div>
              <EffectiveNotes rows={Object.values(effective)} />
              <div className="grid max-h-64 grid-cols-1 gap-1 overflow-y-auto sm:grid-cols-2">
                {candidates.map((name) => (
                  <ToolCell
                    effective={effective[name]}
                    key={name}
                    missing={!tools.includes(name)}
                    name={name}
                    onToggle={() => toggle(name)}
                    picked={picked.includes(name)}
                  />
                ))}
              </div>
            </>
          )}

          <SaveBar
            busy={busy}
            dirty={dirty}
            label="保存授权"
            note={message || (dirty ? '有未保存的改动' : '与服务器一致')}
            onReset={() => {
              setMode(saved.mode);
              setAllow(saved.allow);
              setDeny(saved.deny);
              setMessage('');
            }}
            onSave={() => void save()}
          />
        </div>
      )}
    </Item>
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
      description="白名单管的是这个节点自己能调哪些工具。没授权的工具不会出现在给模型的清单里，模型不知道它存在。"
      scope="all-channels"
      title="节点授权"
    >
      <DefaultsNotice nodes={nodes} />
      {nodes.length === 0 ? <Empty>{message || '正在读取…'}</Empty> : (
        <List>
          {nodes.map((node) => (
            <NodeRow key={node.id} node={node} onSaved={() => void load()} tools={tools} />
          ))}
        </List>
      )}
      <Footnote>
        能派活给谁是另一条线，由节点 YAML 的 delegate_targets 决定，和这里的白名单同时生效：
        这里没给 execute_command，它照样可以把命令交给 delegate_targets 里列出的下游节点，
        由下游按下游自己那份 tool_access 去执行。想堵死这条路，得把下游一起收紧。
      </Footnote>
      <StatusText message={nodes.length ? message : ''} />
    </Card>
  );
};
