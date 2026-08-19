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
      // 换号期间容器正在重启，读不到是预期中的，不要把它当故障刷屏。
      setAccount((prev) => prev);
      setNote(say(error));
      return null;
    }
  }, [token]);

  useEffect(() => {
    void refresh().then((next) => { if (next) setNote(''); });
  }, [refresh]);

  // 换号过程要一直盯着：先等容器活过来拿二维码，再等扫码成功把号钉住。
  useEffect(() => {
    if (stage === 'idle' || !token) return undefined;
    let alive = true;
    const timer = window.setInterval(() => {
      void (async () => {
        if (!alive) return;
        if (Date.now() - stageStartedAt.current > RESTART_TIMEOUT_MS) {
          setStage('idle');
          setNote('等待超时。NapCat 可能没起来，去服务器上看看容器状态。');
          return;
        }
        const next = await refresh();
        if (!alive || !next) return;

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
            const raw = await getQqLoginQrcode(token);
            if (!raw || !alive) return;
            setQrImage(await QRCode.toDataURL(raw, { width: 240, margin: 1 }));
            setStage('scanning');
            setNote('用要登录的那个 QQ 扫码。二维码会过期，过期就点一下「刷新二维码」。');
          } catch {
            setNote('容器重启中，等它起来…');
          }
        }
      })();
    }, POLL_MS);
    return () => { alive = false; window.clearInterval(timer); };
  }, [stage, token, qrImage, refresh]);

  const startRelogin = async () => {
    if (!token) return;
    const current = account?.nick || account?.uin || '当前账号';
    if (!window.confirm(
      `确定要换号吗？\n\n${current} 会立刻下线，NapCat 容器重启后停在等扫码状态，`
      + '期间 bot 完全不可用。新号如果不在原来的群里，群名单和管理员名单都要重配。',
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

  const switchTo = async (uin: string) => {
    if (!token) return;
    if (!window.confirm(`切换到 ${uin}？当前账号会下线。`)) return;
    setBusy(true);
    try {
      await qqQuickLogin(token, uin);
      stageStartedAt.current = Date.now();
      setStage('restarting');
      setNote('已请求切换，等它上线…');
    } catch (error) {
      setNote(say(error));
    }
    setBusy(false);
  };

  const refreshQrcode = async () => {
    if (!token) return;
    setBusy(true);
    try {
      const raw = await getQqLoginQrcode(token);
      setQrImage(raw ? await QRCode.toDataURL(raw, { width: 240, margin: 1 }) : '');
      setNote(raw ? '' : '拿不到二维码，可能已经登录上了。');
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
                    {account.is_login ? (account.online ? '在线' : '已登录，但不在线') : '未登录'}
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
            account.quick_login.map((uin) => (
              <div className="qc-chan-row" key={uin}>
                <span className="qc-chan-label"><code>{uin}</code></span>
                <div className="qc-chan-body">
                  <button
                    className="qc-btn qc-btn-quiet"
                    disabled={busy || uin === account.uin}
                    onClick={() => void switchTo(uin)}
                    type="button"
                  >
                    {uin === account.uin ? '当前账号' : '切到这个号'}
                  </button>
                </div>
              </div>
            ))
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
