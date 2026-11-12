// 链路分区里前端自己画的几块：队列与缓存的实况、改一次要重启的那几个值，以及唯一的危险操作。
import { useState } from 'react';

import { receipted, restartEngine } from '../api/supervisorClient';
import { usePageNotice } from '../hooks/usePageNotice';
import { useSettingsStore } from '../store/settingsStore';
import { restartRefusal, unreadRestartMessage } from '../utils/systemStatus';
import {
  Button,
  DangerPanel,
  Desc,
  ErrorText,
  Facts,
  Panel,
  ReadOnlyRow,
} from './components';
import { useLinkFacts, useLiveValue, useRuntime } from './consoleStore';
import { errorMessage } from '../api/request';

export const QueueFacts = () => {
  const runtime = useRuntime();
  const link = useLinkFacts();
  const wanted = useLiveValue<number>('queue_workers', 0);
  const enabled = useLiveValue<boolean>('enable_queue', false);
  const running = runtime.queue_workers_running ?? 0;
  const pending = link.backlog;
  if (!enabled) return null;
  return (
    <Facts>
      实际在跑 {running} 个 worker{running !== wanted && '，正在向配置对齐'}，队列里积压 {pending} 条。
    </Facts>
  );
};

export const HistoryFacts = () => {
  const runtime = useRuntime();
  const caps = runtime.group_history_capacities || [];
  const cached = runtime.volatile?.cached_groups ?? 0;
  const gaps = runtime.volatile?.history_gap_groups ?? 0;
  if (!cached) return <Facts>还没有群的历史被缓存。</Facts>;
  return (
    <Facts>
      已缓存 {cached} 个群
      {caps.length === 1 ? `，容量 ${caps[0]} 条` : caps.length > 1 ? `，容量还在对齐（${caps.join(' / ')}）` : ''}。
      {gaps > 0 && `${gaps} 个群有历史被缓存上限挤掉，模型那边会看到「此前 N 条未包含」的说明。`}
    </Facts>
  );
};

export const BridgeFacts = () => {
  const runtime = useRuntime();
  const enabled = useLiveValue<boolean>('enable_forward_bridge', true);
  if (!enabled) return null;
  return (
    <Facts>
      {runtime.forward_bridge_running ? '正在监听' : '尚未启动'}
      {runtime.forward_bridge_endpoint && (
        <> <code className="font-mono text-mini">{runtime.forward_bridge_endpoint}</code></>
      )}
      ，令牌{runtime.forward_bridge_token_set ? '已设置' : '未设置'}。
    </Facts>
  );
};

const RECOVERY_STATES: Record<string, string> = {
  idle: '空闲',
  waiting: '等链路恢复',
  relogin: '正在快速登录',
  login_mode: '已进扫码态',
  exhausted: '预算用完，等人处理',
  manual: '不自动处理',
};

const OFFLINE_REASONS: Record<string, string> = {
  session_invalid: '登录态被服务端注销',
  other_device: '在别的设备上被踢',
  password_changed: '密码已改',
  low_version: '版本过低',
  link_down: '链路断开',
  unknown: '原因不明',
};

const clock = (stamp: number) => new Date(stamp * 1000).toLocaleString();

export const RecoveryFacts = () => {
  const link = useLinkFacts();
  const enabled = useLiveValue<boolean>('recovery_enabled', true);
  const limit = useLiveValue<number>('recovery_daily_limit', 0);
  const { recovery } = link;
  if (!link.connected) return <Facts>还没连上 NapCat。</Facts>;
  const budget = `24 小时内已用 ${recovery.actions_used_24h}/${limit} 次自动登录`;
  if (link.online) {
    return (
      <Facts>
        登录态在线。{enabled ? budget : '自动恢复已关闭'}
        {link.last_offline_at > 0 && `；上次掉线 ${clock(link.last_offline_at)}`}
        {recovery.note && `，${recovery.note}`}。
      </Facts>
    );
  }
  return (
    <Facts>
      <strong>离线</strong>：{OFFLINE_REASONS[link.offline_reason] || link.offline_reason || '原因未知'}
      {link.last_offline_at > 0 && `（${clock(link.last_offline_at)}）`}
      ，恢复状态 {RECOVERY_STATES[recovery.state] || recovery.state || '未知'}
      {recovery.note && `：${recovery.note}`}。{enabled ? budget : '自动恢复已关闭'}。
    </Facts>
  );
};

export const Environment = () => {
  const runtime = useRuntime();
  const env = runtime.environment || {};
  return (
    <Panel>
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
    </Panel>
  );
};

export const RestartEngine = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [armed, setArmed] = useState(false);
  const { notice, error, setMessage, setError } = usePageNotice();

  const fire = async () => {
    if (!token) return;
    setMessage('正在请求…');
    try {
      const { result, receiptProblem } = await receipted(restartEngine(token, '用户从控制台请求重启引擎'));
      // 回执读不懂时请求已经到了：当成失败，人会再点一次，可能重启两回。
      if (!result) setMessage(unreadRestartMessage(receiptProblem));
      else if (result.scheduled) setMessage('已排上重启，几秒后 worker 会重新起来。');
      else setError(`没能重启：${restartRefusal(result)}。`);
    } catch (caught) {
      setError(errorMessage(caught, '请求失败'));
    }
    setArmed(false);
  };

  return (
    <DangerPanel>
      <div>
        <p className="font-medium">重启 engine worker</p>
        <Desc indent={false}>
          只重启跑模型的 worker，调度器和这个界面不受影响。正在跑的任务会被中断，QQ 那边表现为没有回音。
        </Desc>
      </div>
      {armed ? (
        <span className="inline-flex gap-2">
          <Button onClick={() => setArmed(false)} tone="quiet">算了</Button>
          <Button onClick={() => void fire()} tone="danger">确认重启</Button>
        </span>
      ) : (
        <Button onClick={() => setArmed(true)} tone="danger">重启</Button>
      )}
      {error && <ErrorText className="basis-full" role="alert">{error}</ErrorText>}
      {notice && <Facts className="basis-full" role="status">{notice}</Facts>}
    </DangerPanel>
  );
};
