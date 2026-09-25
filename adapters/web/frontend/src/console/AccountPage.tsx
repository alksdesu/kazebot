// 两件容易混的事：换这一个实例登录的号（下面的扫码与快速切换），和这台机器上
// 同时跑着几个号（多开，各有各的进程与数据）。NapCat 只监听本机、且带自己的
// token，所有账号动作都由 supervisor 代转。
import { useCallback, useEffect, useRef, useState } from 'react';
import QRCode from 'qrcode';

import {
  createInstance,
  deleteInstance,
  getInstanceProgress,
  getInstances,
  getQqAccount,
  getQqLoginQrcode,
  ownedByAnotherInstance,
  qqEnterLoginMode,
  qqPinAccount,
  qqQuickLogin,
  type ConsoleInstance,
  type InstanceProgress,
  type QqAccount,
  type QqQuickLoginTarget,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Button, Empty, Facts, Input, Panel, Pre } from './components';
import { InstanceLink, InstanceSwitch } from './InstanceSwitch';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { useRequestScope } from '../features/asyncState';

type Stage = 'idle' | 'restarting' | 'scanning';

const POLL_MS = 3000;
// 容器重启到 WebUI 能应答通常十几秒，给足余量再放弃。
const RESTART_TIMEOUT_MS = 180000;

const ROW = 'mt-1.5 flex min-w-0 flex-wrap items-center gap-2.5';
const LABEL = 'w-9 flex-none text-xs text-[var(--duties-secondary)]';
const BODY = 'flex min-w-0 flex-1 flex-wrap items-center gap-2';
const DESC = 'text-xs text-[var(--duties-secondary)]';

const say = (error: unknown): string => (error instanceof Error ? error.message : String(error));

export const AccountPage = () => {
  const token = useSettingsStore(state => state.adminToken);
  return token ? <AccountWorkspace key={token} token={token} /> : <Empty>需要管理员令牌。</Empty>;
};

const AccountWorkspace = ({ token }: { token: string }) => {
  const [instances, setInstances] = useState<ConsoleInstance[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  const identity = useRequestScope(token);
  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    setLoading(true);
    try {
      const next = await getInstances(token);
      if (!identity.isCurrent() || request !== sequence.current || useSettingsStore.getState().adminToken !== token) return;
      setInstances(next); setError('');
    } catch (failure) {
      if (identity.isCurrent() && request === sequence.current && useSettingsStore.getState().adminToken === token) setError(say(failure));
    } finally {
      if (identity.isCurrent() && request === sequence.current && useSettingsStore.getState().adminToken === token) setLoading(false);
    }
  }, [token]);
  useEffect(() => { void refresh(); return () => { sequence.current++; }; }, [refresh]);
  return <>
    <InstanceManagement token={token} instances={instances} loading={loading} error={error} refresh={refresh} />
    <AccountConnectionPage token={token} instances={instances} ownershipReady={!loading && !error} />
  </>;
};

interface InstanceManagementProps {
  token: string;
  instances: ConsoleInstance[];
  loading: boolean;
  error: string;
  refresh: () => Promise<void>;
}

const InstanceManagement = ({ token, instances, loading, error, refresh }: InstanceManagementProps) => {
  const [newUin, setNewUin] = useState('');
  const [newLabel, setNewLabel] = useState('');
  const [job, setJob] = useState<InstanceProgress | null>(null);
  const [jobAction, setJobAction] = useState<'create' | 'remove'>('create');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [progressError, setProgressError] = useState('');
  const mutation = useRef(false);
  const identity = useRequestScope(token);
  const isCurrent = () => identity.isCurrent() && useSettingsStore.getState().adminToken === token;
  const running = job !== null && !job.finished;
  const locked = busy || running || loading || Boolean(error);
  useUnsavedChanges(Boolean(newUin || newLabel), '新实例信息尚未提交，切换或离开会丢失这些内容，确定继续吗？');

  useEffect(() => {
    if (!job || job.finished) return;
    let alive = true;
    let polling = false;
    const timer = window.setInterval(() => {
      if (polling || !alive || !isCurrent()) return;
      polling = true;
      void getInstanceProgress(token, job.uin).then(async next => {
        if (!alive || !isCurrent()) return;
        setJob(next); setProgressError('');
        if (next.finished) await refresh();
      }).catch(failure => {
        if (alive && isCurrent()) setProgressError(`读取进度失败，将自动重试：${say(failure)}`);
      }).finally(() => { polling = false; });
    }, 2500);
    return () => { alive = false; window.clearInterval(timer); };
  }, [token, job, refresh]);

  const addInstance = async () => {
    if (!isCurrent() || mutation.current || locked) return;
    const uin = newUin.trim();
    if (!/^[1-9]\d{4,10}$/.test(uin)) { setNote('QQ 号要是 5-11 位数字'); return; }
    if (instances.some(row => row.uin === uin)) { setNote('这个 QQ 号已有独立实例，请直接切换到它的控制台。'); return; }
    if (!window.confirm(`给 ${uin} 建一套独立实例？\n\n会新建 NapCat 容器、systemd 服务和域名路由，约一两分钟。\n建好后要去它自己的控制台扫码登录，当前这个号不受影响。`)) return;
    if (!isCurrent()) return;
    mutation.current = true; setBusy(true); setNote(''); setProgressError('');
    try {
      const plan = await createInstance(token, uin, newLabel.trim());
      if (!isCurrent()) return;
      setNewUin(''); setNewLabel(''); setJobAction('create');
      setJob({ uin: plan.uin, lines: ['已提交，等 root 侧接手…'], finished: false, ok: false, detail: '' });
    } catch (failure) { if (isCurrent()) setNote(say(failure)); }
    finally { mutation.current = false; if (isCurrent()) setBusy(false); }
  };

  const dropInstance = async (row: ConsoleInstance) => {
    if (!isCurrent() || mutation.current || locked || row.current || row.idx === 0) return;
    const typed = window.prompt(`归档实例 ${row.label}（${row.uin}）？\n\n它的进程会停掉，NapCat 容器会被移除，工作区和聊天记录改名归档，不会直接删除。\n确认无误后可自行在服务器上清理。\n\n输入这个 QQ 号确认：`);
    if (typed === null) return;
    if (typed.trim() !== row.uin) { setNote('输入的号对不上，没有执行'); return; }
    if (!isCurrent()) return;
    mutation.current = true; setBusy(true); setNote(''); setProgressError('');
    try {
      await deleteInstance(token, row.uin);
      if (!isCurrent()) return;
      setJobAction('remove');
      setJob({ uin: row.uin, lines: ['已提交停服与归档…'], finished: false, ok: false, detail: '' });
    } catch (failure) { if (isCurrent()) setNote(say(failure)); }
    finally { mutation.current = false; if (isCurrent()) setBusy(false); }
  };

  return <Block title="多开实例" hint="会话、记忆、渠道、人格按实例独立；切换后自动读取该实例的内容，不复制配置。">
    <div className="mb-3 flex flex-wrap items-center gap-3">
      <InstanceSwitch rows={instances} />
      <Button disabled={loading || busy} onClick={() => void refresh()} tone="quiet">刷新实例列表</Button>
    </div>
    {loading && <p role="status" className={DESC}>正在读取实例列表…</p>}
    {error && <p role="alert" className="mb-3 text-sm text-[var(--duties-danger)]">{error}</p>}
    <Panel>
      {!loading && !error && !instances.length && <Facts>
        还没启用多开。在服务器上执行一次 <code>sudo deploy/install_provision.sh &lt;当前QQ号&gt;</code>，之后这里就能直接加号。
      </Facts>}
      {instances.map(row => <div className={ROW} key={row.uin}>
        <span className="flex min-w-0 flex-col">
          <strong className="break-all text-sm font-medium">{row.label}</strong>
          <code className={DESC}>{row.uin}</code>
        </span>
        <div className={BODY}>
          {row.current ? <span className={DESC}>当前实例</span> : <>
            <InstanceLink className="flex-none" path={row.path} tone="quiet">去它的控制台</InstanceLink>
            {row.idx !== 0 && <Button disabled={locked} onClick={() => void dropInstance(row)} tone="danger">停用并归档</Button>}
          </>}
        </div>
      </div>)}
      {instances.length > 0 && <div className="mt-4 space-y-3 border-t border-[var(--duties-border)] pt-4">
        <label className="flex min-w-0 flex-col gap-1 text-sm">
          新实例 QQ 号
          <Input aria-label="新实例 QQ 号" disabled={locked} inputMode="numeric" onChange={event => setNewUin(event.target.value)} placeholder="QQ 号" value={newUin} width="wide" />
        </label>
        <label className="flex min-w-0 flex-col gap-1 text-sm">
          实例备注
          <Input aria-label="实例备注" disabled={locked} onChange={event => setNewLabel(event.target.value)} placeholder="备注，可留空" value={newLabel} width="wide" />
        </label>
        <Button disabled={locked || !newUin.trim()} onClick={() => void addInstance()} tone="quiet">建实例</Button>
      </div>}
      {note && <p role="alert" className="mt-3 text-sm text-[var(--duties-danger)]">{note}</p>}
      {job && <>
        <Pre className="mt-3 max-h-60">{job.lines.join('\n')}</Pre>
        {progressError && <p role="alert" className="mt-3 text-sm text-[var(--duties-danger)]">{progressError}</p>}
        {job.finished && <Facts>{job.ok
          ? jobAction === 'create' ? '实例已建立，请去它自己的控制台扫码登录。' : '实例已停用，工作区和聊天记录已归档。'
          : `没成功：${job.detail || '看上面的日志'}`}</Facts>}
      </>}
    </Panel>
  </Block>;
};

const AccountConnectionPage = ({ token, instances, ownershipReady }: { token: string; instances: ConsoleInstance[]; ownershipReady: boolean }) => {
  const identity = useRequestScope(token);
  const isCurrent = () => identity.isCurrent() && useSettingsStore.getState().adminToken === token;
  const mutation = useRef(false);
  const refreshSequence = useRef(0);
  const [account, setAccount] = useState<QqAccount | null>(null);
  const [stage, setStage] = useState<Stage>('idle');
  const [qrImage, setQrImage] = useState('');
  const [note, setNote] = useState('正在读取…');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const stageStartedAt = useRef(0);

  const refresh = useCallback(async (): Promise<QqAccount | null> => {
    if (!isCurrent()) return null;
    const request = ++refreshSequence.current;
    setLoading(true);
    try {
      const next = await getQqAccount(token);
      if (!isCurrent() || request !== refreshSequence.current) return null;
      setAccount(next);
      setNote('');
      return next;
    } catch (error) {
      // 只有网络层或鉴权失败才到这里。NapCat 没起来是正常响应，由 reachable 表达。
      if (isCurrent() && request === refreshSequence.current) setNote(say(error));
      return null;
    } finally {
      if (isCurrent() && request === refreshSequence.current) setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
    return () => { refreshSequence.current++; };
  }, [refresh]);

  // 没登录上就一直盯着：等容器应答、等二维码、等扫码结果。首次部署进页面就是
  // 这个状态，早先只在换号时才轮询，于是第一次进来只能靠人反复手点。
  const watching = account?.configured === true && (account.reachable === false || !account.is_login);

  useEffect(() => {
    if (!token || (stage === 'idle' && !watching)) return undefined;
    let alive = true;
    let polling = false;
    const timer = window.setInterval(() => {
      void (async () => {
        if (!alive || !isCurrent() || polling || mutation.current) return;
        polling = true;
        try {
          if (stage !== 'idle' && Date.now() - stageStartedAt.current > RESTART_TIMEOUT_MS) {
            setStage('idle');
            setNote('等待超时。NapCat 可能没起来，去服务器上看看容器状态。');
            return;
          }
          const next = await refresh();
          if (!alive || !isCurrent() || !next) return;

          if (next.reachable === false) {
            setNote('NapCat 还没应答，等它起来…');
            return;
          }

          if (next.is_login && next.uin) {
            setQrImage('');
            setStage('idle');
            try {
              await qqPinAccount(token);
              if (isCurrent()) setNote(`已登录 ${next.nick || next.uin}，并设为重启后自动登录。`);
            } catch (error) {
              if (isCurrent()) setNote(`已登录 ${next.nick || next.uin}，但设置自动登录失败：${say(error)}`);
            }
            return;
          }

          if (stage === 'restarting' || !qrImage) {
            try {
              // CheckLoginStatus 在等扫码时就带着二维码，能省一次请求。
              const raw = next.qrcode || (await getQqLoginQrcode(token)).qrcode;
              if (!raw || !alive || !isCurrent()) return;
              const image = await QRCode.toDataURL(raw, { width: 240, margin: 1 });
              if (!alive || !isCurrent()) return;
              setQrImage(image);
              setStage('scanning');
              setNote('用要登录的那个 QQ 扫码。二维码会过期，过期就点一下「刷新二维码」。');
            } catch (error) {
              if (alive && isCurrent()) setNote(say(error));
            }
          }
        } finally { polling = false; }
      })();
    }, POLL_MS);
    return () => { alive = false; window.clearInterval(timer); };
  }, [stage, token, qrImage, refresh, watching]);

  const startRelogin = async () => {
    if (!isCurrent() || mutation.current || stage !== 'idle' || !account?.configured) return;
    const current = account?.nick || account?.uin || '当前账号';
    if (!window.confirm(
      `确定要换号吗？\n\n${current} 会立刻下线，NapCat 容器重启后停在等扫码状态，`
      + '期间 bot 完全不可用。新号如果不在原来的群里，群名单和管理员名单都要重配。\n\n'
      + '新号的会话和长期记忆从零开始，各号各记各的。想把这个号的数据带过去，'
      + '去「记忆」页搬迁；换回来时旧数据会自动回来。\n\n'
      + '如果只是想让两个号同时在线，要的是多开而不是换号——见本页「多开实例」。',
    )) return;
    if (!isCurrent()) return;
    mutation.current = true; refreshSequence.current++; setLoading(false); setBusy(true);
    setQrImage('');
    setNote('正在重启 NapCat…');
    try {
      await qqEnterLoginMode(token);
      if (!isCurrent()) return;
      stageStartedAt.current = Date.now();
      setStage('restarting');
    } catch (error) {
      if (isCurrent()) setNote(say(error));
    } finally {
      mutation.current = false;
      if (isCurrent()) setBusy(false);
    }
  };

  const switchTo = async (target: QqQuickLoginTarget) => {
    if (!isCurrent() || mutation.current || !ownershipReady || stage !== 'idle' || target.available === false || target.uin === account?.uin || ownedByAnotherInstance(target.uin, instances)) return;
    const { uin } = target;
    const who = target.nick ? `${target.nick}（${uin}）` : uin;
    if (!window.confirm(`切换到 ${who}？当前账号会下线，会话和长期记忆各号各算。`)) return;
    if (!isCurrent()) return;
    mutation.current = true; refreshSequence.current++; setLoading(false); setBusy(true);
    try {
      await qqQuickLogin(token, uin);
      if (!isCurrent()) return;
      stageStartedAt.current = Date.now();
      setStage('restarting');
      setNote('已请求切换，等它上线…');
    } catch (error) {
      if (!isCurrent()) return;
      setNote(say(error));
      // 切失败会把这个号记成死号，刷一次列表让它立刻置灰。
      void refresh();
    } finally {
      mutation.current = false;
      if (isCurrent()) setBusy(false);
    }
  };

  const refreshQrcode = async () => {
    if (!isCurrent() || mutation.current) return;
    mutation.current = true; setBusy(true);
    try {
      const { qrcode, reachable } = await getQqLoginQrcode(token);
      if (!isCurrent()) return;
      const image = qrcode ? await QRCode.toDataURL(qrcode, { width: 240, margin: 1 }) : '';
      if (!isCurrent()) return;
      setQrImage(image);
      if (qrcode) setNote('');
      else setNote(reachable ? '拿不到二维码，可能已经登录上了。' : 'NapCat 还没应答，等它起来…');
    } catch (error) {
      if (isCurrent()) setNote(say(error));
    } finally {
      mutation.current = false;
      if (isCurrent()) setBusy(false);
    }
  };

  if (!token) return <Empty>需要管理员令牌。</Empty>;

  if (account && !account.configured) {
    return (
      <Block hint="supervisor 拿不到 NapCat 的 WebUI token" title="机器人账号">
        <Panel>
          <Facts>
            请把 NapCat 容器里 <code className="text-[0.65rem]">/app/napcat/config/webui.json</code> 的 token 写进工作区
            <code className="text-[0.65rem]">.env</code> 的 <code className="text-[0.65rem]">NAPCAT_WEBUI_TOKEN</code>，或让模型用 manage_secret 设置。
          </Facts>
          <Button disabled={loading} onClick={() => void refresh()} tone="quiet">刷新账号状态</Button>
        </Panel>
      </Block>
    );
  }

  return (
    <>
      <Block hint="这一个实例现在用哪个号在说话" title="机器人账号">
        <Panel>
          {account ? (
            <>
              <div className={ROW}>
                <span className={LABEL}>账号</span>
                <div className={BODY}>
                  <code className="text-xs">{account.uin || '未登录'}</code>
                  {account.nick && <span className={DESC}>{account.nick}</span>}
                </div>
              </div>
              <div className={ROW}>
                <span className={LABEL}>状态</span>
                <div className={BODY}>
                  <span className={DESC}>
                    {account.reachable === false
                      ? 'NapCat 没应答，容器可能正在重启'
                      : account.is_login
                        ? (account.online ? '在线' : '已登录，但不在线')
                        : '未登录'}
                    {account.login_error ? ` — ${account.login_error}` : ''}
                  </span>
                </div>
              </div>
            </>
          ) : (
            <Facts>{note || '正在读取…'}</Facts>
          )}
          <div className={ROW}>
            <span className={LABEL} />
            <div className={BODY}>
              <Button
                className="flex-none"
                disabled={busy || loading || stage !== 'idle' || !account}
                onClick={() => void startRelogin()}
                tone="quiet"
              >
                换个号登录
              </Button>
              <Button disabled={busy || loading} onClick={() => void refresh()} tone="quiet">刷新账号状态</Button>
              <span className={DESC}>会重启 NapCat，bot 期间不可用</span>
            </div>
          </div>
        </Panel>
      </Block>

      {(stage !== 'idle' || qrImage) && (
        <Block hint="用要登录的那个 QQ 扫" title="扫码登录">
          <Panel>
            {qrImage ? (
              <>
                <img alt="QQ 登录二维码" height={240} src={qrImage} width={240} />
                <Facts>扫完不用管，这一页会自己确认登录结果。</Facts>
              </>
            ) : (
              <Facts>{note || '等 NapCat 起来…'}</Facts>
            )}
            <div className={ROW}>
              <span className={LABEL} />
              <div className={BODY}>
                <Button className="flex-none" disabled={busy} onClick={() => void refreshQrcode()} tone="quiet">
                  刷新二维码
                </Button>
                <Button
                  className="flex-none"
                  disabled={busy}
                  onClick={() => { setStage('idle'); setQrImage(''); setNote(''); }}
                  tone="quiet"
                >
                  停止等待
                </Button>
              </div>
            </div>
          </Panel>
        </Block>
      )}

      <Block hint="换这一个实例登录的号，免扫码。数据不跟着走" title="快速切换">
        <Panel>
          {account?.quick_login?.length ? (
            account.quick_login.map((target) => {
              const current = target.uin === account.uin;
              const usable = target.available !== false;
              const elsewhere = ownedByAnotherInstance(target.uin, instances);
              return (
                <div className={ROW} key={target.uin}>
                  <span className="flex min-w-0 flex-none items-center gap-2">
                    {target.avatar && (
                      <img
                        alt=""
                        className="h-7 w-7 flex-none rounded-full bg-[var(--duties-muted)] object-cover"
                        referrerPolicy="no-referrer"
                        src={target.avatar}
                      />
                    )}
                    <span className="flex min-w-0 flex-col">
                      {target.nick && (
                        <strong className="overflow-hidden text-ellipsis whitespace-nowrap text-xs font-medium">
                          {target.nick}
                        </strong>
                      )}
                      <code className={DESC}>{target.uin}</code>
                    </span>
                  </span>
                  <div className={BODY}>
                    {elsewhere ? (
                      <>
                        <InstanceLink className="flex-none" path={elsewhere.path} tone="quiet">
                          去它的控制台
                        </InstanceLink>
                        <span className={DESC}>这个号有自己的实例，不能从这里登</span>
                      </>
                    ) : (
                      <>
                        <Button
                          className="flex-none"
                          disabled={busy || stage !== 'idle' || current || !usable || !ownershipReady}
                          onClick={() => void switchTo(target)}
                          tone="quiet"
                        >
                          {current ? '当前账号' : '切到这个号'}
                        </Button>
                        {!current && !usable && (
                          <span className={DESC} title={target.dead_reason || ''}>
                            登录态已失效，只能扫码
                          </span>
                        )}
                      </>
                    )}
                  </div>
                </div>
              );
            })
          ) : (
            <Facts>
              没有可免扫码切换的号。一个号在这台机器上登录过之后才会出现在这里。
            </Facts>
          )}
        </Panel>
      </Block>

      {!ownershipReady && account?.quick_login?.length ? <Facts>实例归属尚未确认，请先刷新实例列表后再快速登录。</Facts> : null}

      {note && account && <Facts>{note}</Facts>}
    </>
  );
};
