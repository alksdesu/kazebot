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
  instanceConsoleHref,
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
import { Block, Button, Empty, Facts, Input, LinkButton, Panel, Pre } from './components';
import { InstanceSwitch } from './InstanceSwitch';

type Stage = 'idle' | 'restarting' | 'scanning';

const POLL_MS = 3000;
// 容器重启到 WebUI 能应答通常十几秒，给足余量再放弃。
const RESTART_TIMEOUT_MS = 180000;

const ROW = 'mt-1.5 flex items-center gap-2.5';
const LABEL = 'w-9 flex-none text-xs text-[var(--duties-secondary)]';
const BODY = 'flex min-w-0 flex-1 items-center gap-2';
const DESC = 'text-xs text-[var(--duties-secondary)]';

const say = (error: unknown): string => (error instanceof Error ? error.message : String(error));

export const AccountPage = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [account, setAccount] = useState<QqAccount | null>(null);
  const [stage, setStage] = useState<Stage>('idle');
  const [qrImage, setQrImage] = useState('');
  const [note, setNote] = useState('正在读取…');
  const [busy, setBusy] = useState(false);
  const [instances, setInstances] = useState<ConsoleInstance[]>([]);
  const [newUin, setNewUin] = useState('');
  const [newLabel, setNewLabel] = useState('');
  const [job, setJob] = useState<InstanceProgress | null>(null);
  const stageStartedAt = useRef(0);

  const refresh = useCallback(async (): Promise<QqAccount | null> => {
    if (!token) return null;
    try {
      const next = await getQqAccount(token);
      setAccount(next);
      return next;
    } catch (error) {
      // 只有网络层或鉴权失败才到这里。NapCat 没起来是正常响应，由 reachable 表达。
      setAccount((prev) => prev);
      setNote(say(error));
      return null;
    }
  }, [token]);

  useEffect(() => {
    void refresh().then((next) => { if (next) setNote(''); });
  }, [refresh]);

  useEffect(() => {
    if (!token) return;
    // 读不到就当单实例：宁可少一块说明，也不能因为清单缺失把所有号都禁掉。
    void getInstances(token).then(setInstances).catch(() => setInstances([]));
  }, [token]);

  // 没登录上就一直盯着：等容器应答、等二维码、等扫码结果。首次部署进页面就是
  // 这个状态，早先只在换号时才轮询，于是第一次进来只能靠人反复手点。
  const watching = account !== null && (account.reachable === false || !account.is_login);

  useEffect(() => {
    if (!token || (stage === 'idle' && !watching)) return undefined;
    let alive = true;
    const timer = window.setInterval(() => {
      void (async () => {
        if (!alive) return;
        if (stage !== 'idle' && Date.now() - stageStartedAt.current > RESTART_TIMEOUT_MS) {
          setStage('idle');
          setNote('等待超时。NapCat 可能没起来，去服务器上看看容器状态。');
          return;
        }
        const next = await refresh();
        if (!alive || !next) return;

        if (next.reachable === false) {
          setNote('NapCat 还没应答，等它起来…');
          return;
        }

        if (next.is_login && next.uin) {
          setQrImage('');
          setStage('idle');
          try {
            await qqPinAccount(token);
            setNote(`已登录 ${next.nick || next.uin}，并设为重启后自动登录。`);
          } catch (error) {
            setNote(`已登录 ${next.nick || next.uin}，但设置自动登录失败：${say(error)}`);
          }
          return;
        }

        if (stage === 'restarting' || !qrImage) {
          try {
            // CheckLoginStatus 在等扫码时就带着二维码，能省一次请求。
            const raw = next.qrcode || (await getQqLoginQrcode(token)).qrcode;
            if (!raw || !alive) return;
            setQrImage(await QRCode.toDataURL(raw, { width: 240, margin: 1 }));
            setStage('scanning');
            setNote('用要登录的那个 QQ 扫码。二维码会过期，过期就点一下「刷新二维码」。');
          } catch (error) {
            setNote(say(error));
          }
        }
      })();
    }, POLL_MS);
    return () => { alive = false; window.clearInterval(timer); };
  }, [stage, token, qrImage, refresh, watching]);

  const startRelogin = async () => {
    if (!token) return;
    const current = account?.nick || account?.uin || '当前账号';
    if (!window.confirm(
      `确定要换号吗？\n\n${current} 会立刻下线，NapCat 容器重启后停在等扫码状态，`
      + '期间 bot 完全不可用。新号如果不在原来的群里，群名单和管理员名单都要重配。\n\n'
      + '新号的会话和长期记忆从零开始，各号各记各的。想把这个号的数据带过去，'
      + '去「记忆」页搬迁；换回来时旧数据会自动回来。\n\n'
      + '如果只是想让两个号同时在线，要的是多开而不是换号——见本页最下面那一块。',
    )) return;
    setBusy(true);
    setQrImage('');
    setNote('正在重启 NapCat…');
    try {
      await qqEnterLoginMode(token);
      stageStartedAt.current = Date.now();
      setStage('restarting');
    } catch (error) {
      setNote(say(error));
    }
    setBusy(false);
  };

  const switchTo = async (target: QqQuickLoginTarget) => {
    if (!token) return;
    const { uin } = target;
    const who = target.nick ? `${target.nick}（${uin}）` : uin;
    if (!window.confirm(`切换到 ${who}？当前账号会下线，会话和长期记忆各号各算。`)) return;
    setBusy(true);
    try {
      await qqQuickLogin(token, uin);
      stageStartedAt.current = Date.now();
      setStage('restarting');
      setNote('已请求切换，等它上线…');
    } catch (error) {
      setNote(say(error));
      // 切失败会把这个号记成死号，刷一次列表让它立刻置灰。
      void refresh();
    }
    setBusy(false);
  };

  // root 侧那一串动作要一两分钟，进度只能靠轮询日志文件。
  useEffect(() => {
    if (!token || !job || job.finished) return undefined;
    const timer = window.setInterval(() => {
      void getInstanceProgress(token, job.uin)
        .then((next) => {
          setJob(next);
          // 建完/删完清单才变，这时候刷一次列表就够，不必一直拉。
          if (next.finished) void getInstances(token).then(setInstances).catch(() => {});
        })
        .catch(() => {});
    }, 2500);
    return () => window.clearInterval(timer);
  }, [token, job]);

  const addInstance = async () => {
    if (!token) return;
    const uin = newUin.trim();
    if (!/^[1-9]\d{4,10}$/.test(uin)) {
      setNote('QQ 号要是 5-11 位数字');
      return;
    }
    if (!window.confirm(
      `给 ${uin} 建一套独立实例？\n\n`
      + '会新建 NapCat 容器、systemd 服务和域名路由，约一两分钟。\n'
      + '建好后要去它自己的控制台扫码登录，当前这个号不受影响。',
    )) return;
    setBusy(true);
    try {
      const plan = await createInstance(token, uin, newLabel.trim());
      setNewUin('');
      setNewLabel('');
      setJob({ uin: plan.uin, lines: ['已提交，等 root 侧接手…'], finished: false, ok: false, detail: '' });
    } catch (error) {
      setNote(say(error));
    }
    setBusy(false);
  };

  const dropInstance = async (row: ConsoleInstance) => {
    if (!token) return;
    // 手打号码而不是点确定：这一步会停掉一个号的全部服务。
    const typed = window.prompt(
      `删掉实例 ${row.label}（${row.uin}）？\n\n`
      + '它的进程会停掉，NapCat 容器会被移除，工作区和聊天记录改名归档——不是真删，\n'
      + '确认无误后要自己上服务器清理。\n\n输入这个 QQ 号确认：',
    );
    if (typed === null) return;
    if (typed.trim() !== row.uin) {
      setNote('输入的号对不上，没有执行');
      return;
    }
    setBusy(true);
    try {
      await deleteInstance(token, row.uin);
      setJob({ uin: row.uin, lines: ['已提交删除…'], finished: false, ok: false, detail: '' });
    } catch (error) {
      setNote(say(error));
    }
    setBusy(false);
  };

  const refreshQrcode = async () => {
    if (!token) return;
    setBusy(true);
    try {
      const { qrcode, reachable } = await getQqLoginQrcode(token);
      setQrImage(qrcode ? await QRCode.toDataURL(qrcode, { width: 240, margin: 1 }) : '');
      if (qrcode) setNote('');
      else setNote(reachable ? '拿不到二维码，可能已经登录上了。' : 'NapCat 还没应答，等它起来…');
    } catch (error) {
      setNote(say(error));
    }
    setBusy(false);
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
                disabled={busy || stage !== 'idle'}
                onClick={() => void startRelogin()}
                tone="quiet"
              >
                换个号登录
              </Button>
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
                        <LinkButton className="flex-none" href={instanceConsoleHref(elsewhere.path)} tone="quiet">
                          去它的控制台
                        </LinkButton>
                        <span className={DESC}>这个号有自己的实例，不能从这里登</span>
                      </>
                    ) : (
                      <>
                        <Button
                          className="flex-none"
                          disabled={busy || current || !usable}
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

      <Block hint="一个号一套进程，会话、记忆、渠道、人格全部各自一份" title="多开实例">
        {/* 跳到另一个实例。原来长在控制台左窄轨上，那条轨随控制台一起没了。 */}
        <InstanceSwitch />
        <Panel>
          {instances.length === 0 ? (
            <Facts>
              还没启用多开。在服务器上执行一次{' '}
              <code className="text-[0.65rem]">sudo deploy/install_provision.sh {account?.uin || '<当前QQ号>'}</code>
              ，之后这里就能直接加号。
            </Facts>
          ) : (
            instances.map((row) => (
              <div className={ROW} key={row.uin}>
                <span className="flex min-w-0 flex-none items-center gap-2">
                  <span className="flex min-w-0 flex-col">
                    <strong className="overflow-hidden text-ellipsis whitespace-nowrap text-xs font-medium">
                      {row.label}
                    </strong>
                    <code className={DESC}>{row.uin}</code>
                  </span>
                </span>
                <div className={BODY}>
                  {row.current ? (
                    <span className={DESC}>就是这一个</span>
                  ) : (
                    <>
                      <LinkButton className="flex-none" href={instanceConsoleHref(row.path)} tone="quiet">
                        去它的控制台
                      </LinkButton>
                      {row.idx !== 0 && (
                        <Button
                          className="flex-none"
                          disabled={busy || (job !== null && !job.finished)}
                          onClick={() => void dropInstance(row)}
                          tone="danger"
                        >
                          删掉
                        </Button>
                      )}
                    </>
                  )}
                </div>
              </div>
            ))
          )}

          {instances.length > 0 && (
            <div className={ROW}>
              <span className={LABEL}>加号</span>
              <div className={BODY}>
                <Input
                  disabled={busy || (job !== null && !job.finished)}
                  inputMode="numeric"
                  onChange={(event) => setNewUin(event.target.value)}
                  placeholder="QQ 号"
                  value={newUin}
                  width="flex"
                />
                <Input
                  disabled={busy || (job !== null && !job.finished)}
                  onChange={(event) => setNewLabel(event.target.value)}
                  placeholder="备注，可留空"
                  value={newLabel}
                  width="flex"
                />
                <Button
                  className="flex-none"
                  disabled={busy || !newUin.trim() || (job !== null && !job.finished)}
                  onClick={() => void addInstance()}
                  tone="quiet"
                >
                  建实例
                </Button>
              </div>
            </div>
          )}

          {job && (
            <>
              <Pre className="mt-2.5 max-h-60">{job.lines.join('\n')}</Pre>
              {job.finished && (
                <Facts>
                  {job.ok
                    ? '完成了。新号要去它自己的控制台扫码登录。'
                    : `没成功：${job.detail || '看上面的日志'}`}
                </Facts>
              )}
            </>
          )}
        </Panel>
      </Block>

      {note && account && <Facts>{note}</Facts>}
    </>
  );
};
