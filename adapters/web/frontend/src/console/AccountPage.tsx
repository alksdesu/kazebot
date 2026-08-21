// bot 用哪个 QQ 号登录。NapCat 只监听本机、且带自己的 token，所有动作都由 supervisor 代转。
import { useCallback, useEffect, useRef, useState } from 'react';
import QRCode from 'qrcode';

import {
  getQqAccount,
  getQqLoginQrcode,
  qqEnterLoginMode,
  qqPinAccount,
  qqQuickLogin,
  type QqAccount,
  type QqQuickLoginTarget,
} from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Empty } from './components';

type Stage = 'idle' | 'restarting' | 'scanning';

const POLL_MS = 3000;
// 容器重启到 WebUI 能应答通常十几秒，给足余量再放弃。
const RESTART_TIMEOUT_MS = 180000;

const say = (error: unknown): string => (error instanceof Error ? error.message : String(error));

export const AccountPage = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [account, setAccount] = useState<QqAccount | null>(null);
  const [stage, setStage] = useState<Stage>('idle');
  const [qrImage, setQrImage] = useState('');
  const [note, setNote] = useState('正在读取…');
  const [busy, setBusy] = useState(false);
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
      + '去「记忆」页搬迁；换回来时旧数据会自动回来。',
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
        <div className="qc-panel">
          <p className="qc-facts">
            请把 NapCat 容器里 <code>/app/napcat/config/webui.json</code> 的 token 写进工作区
            <code>.env</code> 的 <code>NAPCAT_WEBUI_TOKEN</code>，或让模型用 manage_secret 设置。
          </p>
        </div>
      </Block>
    );
  }

  return (
    <>
      <Block hint="bot 现在用哪个号在说话" title="机器人账号">
        <div className="qc-panel">
          {account ? (
            <>
              <div className="qc-chan-row">
                <span className="qc-chan-label">账号</span>
                <div className="qc-chan-body">
                  <code>{account.uin || '未登录'}</code>
                  {account.nick && <span className="qc-cap-desc">{account.nick}</span>}
                </div>
              </div>
              <div className="qc-chan-row">
                <span className="qc-chan-label">状态</span>
                <div className="qc-chan-body">
                  <span className="qc-cap-desc">
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
            <p className="qc-facts">{note || '正在读取…'}</p>
          )}
          <div className="qc-chan-row">
            <span className="qc-chan-label" />
            <div className="qc-chan-body">
              <button
                className="qc-btn qc-btn-quiet"
                disabled={busy || stage !== 'idle'}
                onClick={() => void startRelogin()}
                type="button"
              >
                换个号登录
              </button>
              <span className="qc-cap-desc">会重启 NapCat，bot 期间不可用</span>
            </div>
          </div>
        </div>
      </Block>

      {(stage !== 'idle' || qrImage) && (
        <Block hint="用要登录的那个 QQ 扫" title="扫码登录">
          <div className="qc-panel">
            {qrImage ? (
              <>
                <img alt="QQ 登录二维码" height={240} src={qrImage} width={240} />
                <p className="qc-facts">扫完不用管，这一页会自己确认登录结果。</p>
              </>
            ) : (
              <p className="qc-facts">{note || '等 NapCat 起来…'}</p>
            )}
            <div className="qc-chan-row">
              <span className="qc-chan-label" />
              <div className="qc-chan-body">
                <button className="qc-btn qc-btn-quiet" disabled={busy} onClick={() => void refreshQrcode()} type="button">
                  刷新二维码
                </button>
                <button className="qc-btn qc-btn-quiet" disabled={busy} onClick={() => { setStage('idle'); setQrImage(''); setNote(''); }} type="button">
                  停止等待
                </button>
              </div>
            </div>
          </div>
        </Block>
      )}

      <Block hint="以前在这台机器上登录过的号，切回去不用扫码" title="快速切换">
        <div className="qc-panel">
          {account?.quick_login?.length ? (
            account.quick_login.map((target) => {
              const current = target.uin === account.uin;
              const usable = target.available !== false;
              return (
                <div className="qc-chan-row" key={target.uin}>
                  <span className="qc-acct-who">
                    {target.avatar && (
                      <img
                        alt=""
                        className="qc-acct-face"
                        referrerPolicy="no-referrer"
                        src={target.avatar}
                      />
                    )}
                    <span className="qc-acct-name">
                      {target.nick && <strong>{target.nick}</strong>}
                      <code>{target.uin}</code>
                    </span>
                  </span>
                  <div className="qc-chan-body">
                    <button
                      className="qc-btn qc-btn-quiet"
                      disabled={busy || current || !usable}
                      onClick={() => void switchTo(target)}
                      type="button"
                    >
                      {current ? '当前账号' : '切到这个号'}
                    </button>
                    {!current && !usable && (
                      <span className="qc-cap-desc" title={target.dead_reason || ''}>
                        登录态已失效，只能扫码
                      </span>
                    )}
                  </div>
                </div>
              );
            })
          ) : (
            <p className="qc-facts">
              没有可免扫码切换的号。一个号在这台机器上登录过之后才会出现在这里。
            </p>
          )}
        </div>
      </Block>

      {note && account && <p className="qc-facts">{note}</p>}
    </>
  );
};
