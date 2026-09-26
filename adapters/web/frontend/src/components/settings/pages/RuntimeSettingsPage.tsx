// 运行诊断：worker 死活、任务队列、日志。
//
// engine 崩了 supervisor 照样活着、systemd 照样 active running，这一页是唯一看得出来的地方。
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  checkHealth,
  getRuntimeStatus,
  listLogFiles,
  readLogTail,
  retryEngine,
  type HealthState,
  type LogFileInfo,
  type RuntimeStatus,
  type WorkerHealth,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Block, Button, Empty, Facts, Footnote, List, PageShell, Pip, Row, StatusText } from './settingsControls';

const POLL_MS = 5000;
const TAIL_LINES = 500;

const say = (error: unknown): string => (error instanceof Error ? error.message : String(error));

const duration = (seconds: number): string => {
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时`;
  return `${Math.floor(seconds / 86400)} 天`;
};

const sizeText = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

/** 调度器自己的死活。它挂了这一页的其余内容全都读不到，所以判据不能只靠带鉴权的接口。 */
const SupervisorBlock = () => {
  const [health, setHealth] = useState<HealthState | null>(null);

  useEffect(() => {
    const probe = () => { void checkHealth().then(setHealth).catch(() => setHealth(null)); };
    probe();
    const timer = setInterval(probe, POLL_MS);
    return () => clearInterval(timer);
  }, []);

  const uptime = health?.uptime_seconds;
  // health.status 是给程序看的字面量（"ok"），别原样摆在中文界面上。
  const state = !health ? '连不上' : health.status === 'ok' ? '在线' : health.status || '在线';

  return (
    <Block hint="控制台、任务路由与审批都跑在它里面" title="Supervisor">
      <List>
        <Row>
          <Pip tone={health ? 'live' : 'halt'} />
          <span className="font-mono text-xs font-semibold">Supervisor</span>
          <span className="font-mono text-xs text-[var(--duties-secondary)]">{state}</span>
          <span className="flex-1" />
          {uptime !== undefined && (
            <span className="font-mono text-micro text-[var(--duties-tertiary)]">已运行 {duration(uptime)}</span>
          )}
        </Row>
      </List>
    </Block>
  );
};

const WorkerRow = ({ name, health }: { name: string; health: WorkerHealth }) => {
  const tone = health.given_up ? 'halt' : health.alive ? 'live' : 'idle';
  const state = health.given_up
    ? '已停止重试'
    : health.alive
      ? `运行中 · ${duration(health.uptime_sec)}`
      : health.retry_in_sec > 0
        ? `${Math.ceil(health.retry_in_sec)} 秒后重拉`
        : '已停止';
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-[var(--duties-border)] px-4 py-3 last:border-b-0">
      <Pip tone={tone} />
      <span className="font-mono text-xs font-semibold">{name}</span>
      {/* 状态灯只有颜色，读屏拿不到，所以这句文字必须自带状态。 */}
      <span className="font-mono text-xs text-[var(--duties-secondary)]">{state}</span>
      <span className="flex-1" />
      {health.pid !== null && (
        <span className="font-mono text-micro text-[var(--duties-tertiary)]">pid {health.pid}</span>
      )}
      {health.generation && (
        <span className="font-mono text-micro text-[var(--duties-tertiary)]">
          gen {health.generation.slice(0, 8)}
        </span>
      )}
      {health.respawns > 0 && (
        <span className="font-mono text-micro text-[var(--duties-tertiary)]">
          重拉过 {health.respawns} 次
        </span>
      )}
    </div>
  );
};

const GaveUpNotice = ({ workers, busy, onRetry }: {
  workers: [string, WorkerHealth][];
  busy: boolean;
  onRetry: () => void;
}) => (
  <section className="border border-[var(--duties-danger)] bg-[var(--duties-panel)] p-4">
    <div className="flex flex-wrap items-center gap-2">
      <h2 className="font-mono text-sm font-semibold text-[var(--duties-danger)]">
        {workers.map(([name]) => name).join('、')} 起不来，已停止重试
      </h2>
      <span className="flex-1" />
      <Button disabled={busy} onClick={onRetry} tone="quiet">{busy ? '正在重试…' : '手动重试'}</Button>
    </div>
    {workers.map(([name, health]) => (
      <div key={name} className="mt-3">
        <p className="font-mono text-xs text-[var(--duties-secondary)]">
          {name} 连续失败 {health.failures} 次
          {health.last_exit_code !== null && `，退出码 ${health.last_exit_code}`}
        </p>
        {health.last_log && (
          <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap break-all border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2 font-mono text-micro leading-5">
            {health.last_log}
          </pre>
        )}
      </div>
    ))}
  </section>
);

const LogViewer = ({ token }: { token: string }) => {
  const [files, setFiles] = useState<LogFileInfo[]>([]);
  const [picked, setPicked] = useState('');
  const [text, setText] = useState('');
  const [truncated, setTruncated] = useState(false);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLPreElement>(null);

  const loadList = useCallback(async () => {
    try {
      const rows = await listLogFiles(token);
      setFiles(rows);
      setPicked((current) => current || rows[0]?.name || '');
    } catch (error) {
      setNote(say(error));
    }
  }, [token]);

  useEffect(() => { void loadList(); }, [loadList]);

  const loadTail = useCallback(async (name: string) => {
    if (!name) return;
    setBusy(true);
    setNote('');
    try {
      const result = await readLogTail(token, name, TAIL_LINES);
      setText(result.text);
      setTruncated(result.truncated);
      // 日志是往下追加的，打开就该看到最新那几行。
      requestAnimationFrame(() => {
        if (box.current) box.current.scrollTop = box.current.scrollHeight;
      });
    } catch (error) {
      setNote(say(error));
    } finally {
      setBusy(false);
    }
  }, [token]);

  useEffect(() => { void loadTail(picked); }, [picked, loadTail]);

  return (
    <Block hint={`最多 ${TAIL_LINES} 行，密钥已在服务端打码`} title="日志">
      {files.length === 0 ? <Empty>还没有日志文件。</Empty> : (
        <div className="grid gap-3 md:grid-cols-[var(--duties-split-columns)]">
          <ul className="max-h-[var(--duties-pane-height)] overflow-y-auto border border-[var(--duties-border)]">
            {files.map((file) => (
              <li key={file.name}>
                <button
                  className={`flex w-full flex-col items-start gap-0.5 border-b border-[var(--duties-border)] px-3 py-2 text-left last:border-b-0 ${
                    file.name === picked
                      ? 'bg-[var(--duties-text)] text-[var(--duties-bg)]'
                      : 'hover:bg-[var(--duties-muted)]'
                  }`}
                  onClick={() => setPicked(file.name)}
                  type="button"
                >
                  <span className="w-full truncate font-mono text-mini">{file.name}</span>
                  <span className="font-mono text-nano opacity-70">{sizeText(file.size)}</span>
                </button>
              </li>
            ))}
          </ul>
          <div className="min-w-0">
            <div className="mb-2 flex items-center gap-2">
              <span className="truncate font-mono text-xs">{picked}</span>
              <span className="flex-1" />
              <Button disabled={busy} onClick={() => void loadTail(picked)} tone="quiet">
                {busy ? '正在刷新…' : '刷新'}
              </Button>
            </div>
            <pre
              className="max-h-[var(--duties-pane-height)] min-h-[var(--duties-pane-min-height)] overflow-auto whitespace-pre-wrap break-all border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 font-mono text-micro leading-5"
              ref={box}
            >
              {text || '（空）'}
            </pre>
            {truncated && <Facts>只显示了尾部，前面的内容请到服务器上看完整文件。</Facts>}
            <StatusText message={note} />
          </div>
        </div>
      )}
    </Block>
  );
};

export const RuntimeSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [note, setNote] = useState('');
  const [retrying, setRetrying] = useState(false);

  const refresh = useCallback(async () => {
    if (!adminToken) return;
    try {
      setStatus(await getRuntimeStatus(adminToken));
      setNote('');
    } catch (error) {
      setNote(say(error));
    }
  }, [adminToken]);

  useEffect(() => {
    if (!adminToken) return;
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [adminToken, refresh]);

  const onRetry = async () => {
    if (!adminToken) return;
    setRetrying(true);
    try {
      await retryEngine(adminToken);
      await refresh();
    } catch (error) {
      setNote(say(error));
    } finally {
      setRetrying(false);
    }
  };

  const workers = Object.entries(status?.workers || {});
  const gaveUp = workers.filter(([, health]) => health.given_up);

  return (
    <PageShell authed={isAuthenticated} description="worker 的死活、任务队列和日志。engine 崩了 supervisor 照样活着，这一页是唯一看得出来的地方。" title="运行">
      <>
        {gaveUp.length > 0 && (
          <GaveUpNotice busy={retrying} onRetry={() => void onRetry()} workers={gaveUp} />
        )}

        <SupervisorBlock />

        <Block
          hint={status ? `supervisor 已运行 ${duration(status.uptime_sec)}` : ''}
          title="Engine Worker"
        >
          {!status ? <Empty>正在读取…</Empty>
            : !status.supervised ? (
              <Empty>这个部署的 engine 不由 supervisor 拉起，这里测不到它的状态。</Empty>
            ) : workers.length === 0 ? <Empty>没有配置 worker。</Empty> : (
              <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)]">
                {workers.map(([name, health]) => (
                  <WorkerRow health={health} key={name} name={name} />
                ))}
              </div>
            )}
          {status && (
            <Facts>
              任务队列：等待 {status.tasks.queued} 个，执行中 {status.tasks.running} 个。
            </Facts>
          )}
          <StatusText message={note} />
        </Block>

        {adminToken && <LogViewer token={adminToken} />}

        <Footnote>
          worker 掉了会自动重拉，头一次立刻拉，之后按 1/2/4/8 秒退避；连续 5 次起不来就停手，
          等你看过原因再手动重试。这一页每 5 秒刷新一次。
        </Footnote>
      </>
    </PageShell>
  );
};
