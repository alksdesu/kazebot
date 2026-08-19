// 运行：谁连上了、队列与缓存的实况、改一次要重启的那几个值，以及唯一的危险操作。
import { useEffect, useState } from 'react';

import { checkHealth, restartEngine, type HealthState } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { Block, Footnote, Grid, Option } from './components';
import { useConsoleStore, useLiveValue, useRuntime } from './consoleStore';
import { BoolOption, NumberField, ReadOnlyRow } from './fields';

const Pip = ({ ok, off }: { ok: boolean; off?: boolean }) => (
  <span aria-hidden="true" className={`qc-pip${ok ? '' : off ? ' qc-pip-idle' : ' qc-pip-halt'}`} />
);

const Link = ({ name, state, detail }: { name: string; state: string; detail: string }) => (
  <div className="qc-link">
    <span className="qc-link-name">{name}</span>
    <span className="qc-link-state">{state}</span>
    <span className="qc-link-detail">{detail}</span>
  </div>
);

const Connections = () => {
  const live = useConsoleStore((state) => state.live);
  const runtime = useRuntime();
  const [health, setHealth] = useState<HealthState | null>(null);

  useEffect(() => { void checkHealth().then(setHealth).catch(() => setHealth(null)); }, []);

  const botAlive = Boolean(live?.published && !live.stale);
  const napcat = Boolean(runtime.onebot_connected);
  const uptime = health?.uptime_seconds;
  // health.status 是给程序看的字面量（"ok"），别原样摆在中文界面上。
  const supervisor = !health ? '连不上' : health.status === 'ok' ? '在线' : health.status || '在线';

  return (
    <div className="qc-links">
      <div className="qc-link-row">
        <Pip ok={Boolean(health)} />
        <Link
          detail={uptime === undefined ? '' : `已运行 ${Math.floor(uptime / 60)} 分钟`}
          name="Supervisor"
          state={supervisor}
        />
      </div>
      <div className="qc-link-row">
        <Pip ok={botAlive} />
        <Link
          detail={runtime.pid ? `pid ${runtime.pid}` : ''}
          name="QQ 适配进程"
          state={botAlive ? '在线' : live?.published ? '心跳中断' : '没在跑'}
        />
      </div>
      <div className="qc-link-row">
        <Pip ok={napcat} off={!botAlive} />
        <Link
          detail={botAlive ? '' : '适配进程没在跑，这一项无从判断'}
          name="NapCat"
          state={!botAlive ? '未知' : napcat ? '已连接' : '未连接'}
        />
      </div>
    </div>
  );
};

const QueueFacts = () => {
  const runtime = useRuntime();
  const wanted = useLiveValue<number>('queue_workers', 0);
  const enabled = useLiveValue<boolean>('enable_queue', false);
  const running = runtime.queue_workers_running ?? 0;
  const pending = runtime.volatile?.queue_pending ?? 0;
  if (!enabled) return null;
  return (
    <p className="qc-facts">
      实际在跑 {running} 个 worker{running !== wanted && '，正在向配置对齐'}，队列里积压 {pending} 条。
    </p>
  );
};

const HistoryFacts = () => {
  const runtime = useRuntime();
  const caps = runtime.group_history_capacities || [];
  const cached = runtime.volatile?.cached_groups ?? 0;
  const gaps = runtime.volatile?.history_gap_groups ?? 0;
  if (!cached) return <p className="qc-facts">还没有群的历史被缓存。</p>;
  return (
    <p className="qc-facts">
      已缓存 {cached} 个群
      {caps.length === 1 ? `，容量 ${caps[0]} 条` : caps.length > 1 ? `，容量还在对齐（${caps.join(' / ')}）` : ''}。
      {gaps > 0 && `${gaps} 个群有历史被缓存上限挤掉，模型那边会看到「此前 N 条未包含」的说明。`}
    </p>
  );
};

const BridgeFacts = () => {
  const runtime = useRuntime();
  const enabled = useLiveValue<boolean>('enable_forward_bridge', true);
  if (!enabled) return null;
  return (
    <p className="qc-facts">
      {runtime.forward_bridge_running ? '正在监听' : '尚未启动'}
      {runtime.forward_bridge_endpoint && <> <code>{runtime.forward_bridge_endpoint}</code></>}
      ，令牌{runtime.forward_bridge_token_set ? '已设置' : '未设置'}。
    </p>
  );
};

const Environment = () => {
  const runtime = useRuntime();
  const env = runtime.environment || {};
  return (
    <div className="qc-panel">
      <ReadOnlyRow name="Supervisor 地址" value={env.supervisor_url || ''} />
      <ReadOnlyRow name="工作目录" value={env.workspace || ''} />
      <ReadOnlyRow
        hint="换掉它，历史匿名别名会全部对不上"
        name="会话哈希盐"
        value={env.hash_secret_set ? '已设置' : '未设置'}
      />
      <ReadOnlyRow
        name="转发桥令牌"
        value={runtime.forward_bridge_token_set ? '已设置' : '未设置'}
      />
    </div>
  );
};

const RestartEngine = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [armed, setArmed] = useState(false);
  const [said, setSaid] = useState('');

  const fire = async () => {
    if (!token) return;
    setSaid('正在请求…');
    try {
      const result = await restartEngine(token, '用户从 QQ 控制台请求重启引擎');
      setSaid(result?.scheduled ? '已排上重启，几秒后 worker 会重新起来。' : '调度器拒绝了这次重启。');
    } catch (error) {
      setSaid(error instanceof Error ? error.message : '请求失败');
    }
    setArmed(false);
  };

  return (
    <div className="qc-danger">
      <div>
        <p className="qc-danger-name">重启 engine worker</p>
        <p className="qc-opt-desc">
          只重启跑模型的 worker，调度器和这个界面不受影响。正在跑的任务会被中断，QQ 那边表现为没有回音。
        </p>
      </div>
      {armed ? (
        <span className="qc-danger-confirm">
          <button className="qc-btn qc-btn-quiet" onClick={() => setArmed(false)} type="button">
            算了
          </button>
          <button className="qc-btn qc-btn-halt" onClick={() => void fire()} type="button">
            确认重启
          </button>
        </span>
      ) : (
        <button className="qc-btn qc-btn-quiet" onClick={() => setArmed(true)} type="button">
          重启
        </button>
      )}
      {said && <p className="qc-facts">{said}</p>}
    </div>
  );
};

export const RuntimePage = () => (
  <>
    <Block hint="三段链路，断一段 bot 就不说话" title="连接">
      <Connections />
    </Block>

    <Block hint="开着的时候群消息排队处理，不再并发" title="消息队列">
      <Grid>
        <BoolOption
          configKey="enable_queue"
          desc="按顺序一条条处理，避免同时开好几个对话。"
          label="启用队列"
        >
          <NumberField configKey="queue_workers" label="worker" unit="个" />
          <NumberField configKey="queue_interval" label="间隔" step={0.5} unit="秒" />
        </BoolOption>
        <BoolOption
          configKey="queue_wait_for_reply"
          desc="等上一条真的回完再取下一条，而不是发出去就算完。"
          label="等回复"
        >
          <NumberField configKey="queue_reply_timeout" label="超时" step={10} unit="秒" />
        </BoolOption>
      </Grid>
      <QueueFacts />
    </Block>

    <Block hint="给模型看的上下文，不是 QQ 的聊天记录" title="群历史">
      <Grid>
        <Option checked disabled name="缓存条数" onChange={() => undefined}>
          <p className="qc-opt-desc">每个群留多少条最近消息。0 表示不缓存。</p>
          <NumberField configKey="group_history_max" label="每群" unit="条" />
        </Option>
        <Option checked disabled name="未送达余量" onChange={() => undefined}>
          <p className="qc-opt-desc">
            还没送到 Engine 的行可以多占几倍位置，硬上限 = 缓存条数 × 本值。1.0 表示不给余量，满了就丢。
          </p>
          <NumberField configKey="group_history_undelivered_ratio" label="上限倍数" step={0.5} unit="倍" />
        </Option>
      </Grid>
      <HistoryFacts />
    </Block>

    <Block hint="本地 HTTP 口子，供别的进程借 bot 发消息" title="转发桥">
      <Grid>
        <BoolOption
          configKey="enable_forward_bridge"
          desc="关掉会立刻停掉这个本地端口。"
          fallback
          label="启用转发桥"
        >
          <NumberField configKey="forward_bridge_max_messages" label="单次最多" unit="条" />
        </BoolOption>
      </Grid>
      <BridgeFacts />
    </Block>

    <Block hint="改这些要重启进程，控制台只读" title="固定配置">
      <Environment />
    </Block>

    <Block hint="唯一会中断正在进行的工作的操作" title="危险操作">
      <RestartEngine />
    </Block>

    <Footnote>
      队列、群历史、转发桥这几项改完同样是热生效 —— bot 会在两秒内重建 worker、deque 与
      监听端口，不需要重启。上面那个重启按钮是给 engine worker 卡住时用的。
    </Footnote>
  </>
);
