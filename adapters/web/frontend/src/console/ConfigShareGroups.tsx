// 共享配置组：几个实例共用整套 config/，记忆各自一份。换的是本实例的 config/ 指向哪里，
// 换完 engine 要重启才会重新加载工具和节点，所以建组、加入、退出之后紧跟一次重启。
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  createShareGroup,
  deleteShareGroup,
  getShareGroups,
  isValidShareGroupName,
  joinShareGroup,
  leaveShareGroup,
  receipted,
  ReceiptUnreadable,
  restartEngine,
  type ShareGroup,
  type ShareGroupChange,
  type ShareGroupOverview,
} from '../api/supervisorClient';
import { useRequestGuard } from '../hooks/useRequestGuard';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { appendSentence } from '../utils/sentence';
import { restartRefusal, unreadRestartMessage } from '../utils/systemStatus';
import { Block, Button, Facts, Footnote, Input, ItemTitle, List, Panel, Row, Tag } from './components';
import { errorMessage } from '../api/request';

const DESC = 'text-xs text-[var(--duties-secondary)]';

type Action = 'create' | 'join' | 'leave' | 'delete';

const ACTION_REASON: Record<Action, string> = {
  create: '建共享配置组',
  join: '加入共享配置组',
  leave: '退出共享配置组',
  delete: '删除共享配置组',
};

// membersKnown 为假时清单读不懂，空的 members 是不知道，不是没人。
const memberLine = (group: ShareGroup, membersKnown: boolean): string => {
  if (!membersKnown) return '查不了';
  return group.members.length
    ? group.members.map(member => (member.current ? `${member.label}（当前实例）` : member.label)).join('、')
    : '还没有成员';
};

const restartMissed = (reason: string): string => `没能重启 engine：${reason}。新配置要等它重启才生效，到「运行」里手动重启一次。`;

const createdLine = (group: ShareGroup): string => {
  const when = group.created_at ? new Date(group.created_at).toLocaleString() : '';
  if (group.created_by && when) return `${group.created_by} 建于 ${when}`;
  return group.created_by ? `${group.created_by} 建的` : when;
};

export const ConfigShareGroups = ({ token }: { token: string }) => {
  const [overview, setOverview] = useState<ShareGroupOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [newName, setNewName] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [problem, setProblem] = useState('');
  const mutation = useRef(false);
  const requests = useRequestGuard();
  useUnsavedChanges(Boolean(newName), '新组还没建，切换或离开会丢掉填好的组名，确定继续吗？');

  const refresh = useCallback(async () => {
    const request = requests.begin('overview');
    setLoading(true);
    try {
      const next = await getShareGroups(token);
      if (!request.isCurrent()) return;
      setOverview(next); setError('');
    } catch (failure) {
      if (request.isCurrent()) setError(errorMessage(failure));
    } finally {
      if (request.isCurrent()) setLoading(false);
    }
  }, [requests, token]);
  useEffect(() => { void refresh(); }, [refresh]);

  // 手动刷新才清掉上一次操作留下的结果和报错；读取失败之后也靠它重来。
  const reload = () => {
    setNote(''); setProblem('');
    void refresh();
  };

  const locked = busy || loading || Boolean(error) || !overview?.available;
  const current = overview?.current ?? null;
  const membersProblem = overview?.members_problem || '';

  /** done 拿到的回执是 null 时，改动已经做完、只是回执读不懂：交代的话里不能用回执上的东西。 */
  const run = async (
    action: Action,
    change: () => Promise<ShareGroupChange>,
    done: (result: ShareGroupChange | null) => string,
  ) => {
    if (!requests.isCurrent() || mutation.current) return;
    mutation.current = true; setBusy(true); setNote(''); setProblem('');
    try {
      let result: ShareGroupChange | null = null;
      let receiptProblem = '';
      try {
        result = await change();
      } catch (failure) {
        if (!requests.isCurrent()) return;
        if (!(failure instanceof ReceiptUnreadable)) { setProblem(errorMessage(failure)); return; }
        receiptProblem = failure.message;
      }
      if (!requests.isCurrent()) return;
      if (result) {
        // 结果里带着切换后的全貌，不必再读一遍；还没回来的旧读取作废，免得它盖掉这份。
        requests.drop('overview');
        setOverview(result.overview);
        setLoading(false);
      } else {
        // 回执里的全貌读不懂，眼下这份是改动之前的：另读一遍。
        void refresh();
      }
      const message = done(result);
      if (action === 'delete') { setNote(message); setProblem(receiptProblem); return; }
      // 配置已经换掉是成功的那半句，照常交代；没重启上是另一回事，单独报错。
      try {
        const { result: restart, receiptProblem: restartProblem } = await receipted(restartEngine(token, ACTION_REASON[action]));
        if (!requests.isCurrent()) return;
        if (!restart) {
          // 重启请求已经到了，只是不知道排没排上：不是失败，跟在成功的那半句后面交代。
          setNote(appendSentence(message, unreadRestartMessage(restartProblem)));
          setProblem(receiptProblem);
        } else if (restart.scheduled) {
          setNote(appendSentence(message, 'engine 正在重启，工具和节点会换成新配置。'));
          setProblem(receiptProblem);
        } else {
          setNote(message);
          setProblem(appendSentence(receiptProblem, restartMissed(restartRefusal(restart))));
        }
      } catch (failure) {
        if (requests.isCurrent()) {
          setNote(message);
          setProblem(appendSentence(receiptProblem, restartMissed(errorMessage(failure))));
        }
      }
    } finally {
      mutation.current = false;
      if (requests.isCurrent()) setBusy(false);
    }
  };

  const archivedText = (result: ShareGroupChange | null): string => (
    result?.archived ? `原来的配置归档在 ${result.archived}。` : ''
  );

  const create = () => {
    const name = newName.trim();
    if (!isValidShareGroupName(name)) { setProblem('组名要以小写字母开头，只用小写字母、数字和下划线，2-32 位'); return; }
    if (overview?.groups.some(group => group.name === name)) { setProblem('已经有叫这个名字的组了，换一个，或者直接加入它。'); return; }
    if (!window.confirm(
      `用本实例现在的配置建共享组「${name}」？\n\n`
      + '本实例的 config/ 会整份抄进组里，.env 里能共享的 API key 抄进组的 secrets.env；'
      + '本实例随即改用组里的配置，原来那份归档到 data/，不删。记忆不共享。\n\n之后会重启本实例的 engine。',
    )) return;
    void run('create', () => createShareGroup(token, name), result => {
      setNewName('');
      const copied = result?.copied_env?.length ? `.env 里的 ${result.copied_env.join('、')} 已抄进组里。` : '';
      return `已建组「${result?.group ?? name}」并加入。${archivedText(result)}${copied}`;
    });
  };

  const join = (group: ShareGroup) => {
    if (!window.confirm(
      `加入共享组「${group.name}」？\n\n`
      + '本实例改用组里的整套配置：人格、技能、工具、MCP、模型渠道与密钥、policy、平台配置都跟组走。'
      + '原来的配置归档到 data/config.archived-*，不会删除。记忆不共享。\n\n之后会重启本实例的 engine。',
    )) return;
    void run('join', () => joinShareGroup(token, group.name), result => `已加入「${result?.group ?? group.name}」。${archivedText(result)}`);
  };

  const leave = () => {
    if (!current) return;
    const leaving = current;
    if (!window.confirm(
      `退出共享组「${leaving}」？\n\n会拷一份组里现在的配置留给本实例接着用，之后两边各改各的。\n\n之后会重启本实例的 engine。`,
    )) return;
    void run('leave', () => leaveShareGroup(token), result => `已退出「${result?.group ?? leaving}」，本实例接着用退出时拷下来的那份配置。`);
  };

  const drop = (group: ShareGroup) => {
    if (!window.confirm(`删除共享组「${group.name}」？\n\n组目录会改名归档，不会直接删除，确认无误后可自行在服务器上清理。`)) return;
    void run('delete', () => deleteShareGroup(token, group.name), result => (
      result ? `已删除「${result.group}」，组目录归档为 ${result.archived}。` : `已删除「${group.name}」，组目录已改名归档。`
    ));
  };

  return <Block title="共享配置" hint="进了同一个组的实例共用整套配置：人格、技能、工具、MCP、模型渠道与密钥、policy、平台配置都在里面，同平台、跨平台都行。记忆永远各自一份。">
    <div className="mb-3 flex flex-wrap items-center gap-3">
      <Button disabled={loading || busy} onClick={reload} tone="quiet">刷新共享组</Button>
    </div>
    {loading && !overview && <p role="status" className={DESC}>正在读取共享配置…</p>}
    {error && <p role="alert" className="mb-3 text-sm text-[var(--duties-danger)]">{error}</p>}
    {overview && !overview.available && <Panel><Facts>{overview.reason}</Facts></Panel>}
    {overview?.available && <>
      <Panel className="mb-3">
        {current ? <div className="flex min-w-0 flex-wrap items-center gap-2.5">
          <span className="text-sm">本实例在共享组</span>
          <Tag tone="live">{current}</Tag>
          <span className="text-sm">里。</span>
          <Button disabled={locked} loading={busy} onClick={leave} tone="quiet">退出这个组</Button>
        </div> : <p className={DESC}>本实例用的是自己的配置，没有和别的实例共享。</p>}
      </Panel>
      {membersProblem && overview.groups.length > 0 && (
        <p role="alert" className="mb-3 text-sm text-[var(--duties-danger)]">
          查不了各组有哪些实例在用：{membersProblem}。修好之前删不了组。
        </p>
      )}
      {overview.groups.length > 0 ? <List>
        {overview.groups.map(group => <Row key={group.name}>
          <span className="flex min-w-0 flex-1 flex-col gap-0.5">
            <ItemTitle className="break-all">{group.name}</ItemTitle>
            <span className={DESC}>成员：{memberLine(group, !membersProblem)}</span>
            {createdLine(group) && <span className={DESC}>{createdLine(group)}</span>}
          </span>
          {group.name === current
            ? <span className={DESC}>本实例在用</span>
            : <span className="flex flex-wrap items-center gap-2">
              {!current && <Button disabled={locked} onClick={() => join(group)} tone="quiet">加入</Button>}
              {!membersProblem && group.members.length === 0 && <Button disabled={locked} onClick={() => drop(group)} tone="danger">删除</Button>}
            </span>}
        </Row>)}
      </List> : <Facts>还没有共享组。</Facts>}
      {!current && <div className="mt-4 space-y-3 border-t border-[var(--duties-border)] pt-4">
        <label className="flex min-w-0 flex-col gap-1 text-sm">
          新共享组名
          <Input aria-label="新共享组名" disabled={locked} onChange={event => setNewName(event.target.value)} placeholder="小写字母开头，比如 main_persona" value={newName} width="wide" />
        </label>
        <Button disabled={locked || !newName.trim()} loading={busy} onClick={create} tone="quiet">用本实例的配置建组</Button>
      </div>}
      {current && <Footnote>要换到别的组，先退出这个组再加入。</Footnote>}
    </>}
    {problem && <p role="alert" className="mt-3 text-sm text-[var(--duties-danger)]">{problem}</p>}
    {note && <Facts role="status">{note}</Facts>}
    {overview && overview.archives.length > 0 && <Footnote>
      本实例加入共享组时归档的旧配置：{overview.archives.join('、')}。要找回来，在服务器上把它挪回 config/（先退出共享组）。
    </Footnote>}
  </Block>;
};
