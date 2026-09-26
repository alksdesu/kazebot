// 链路：队列与缓存的实况、改一次要重启的那几个值，以及唯一的危险操作。
import { useState } from 'react';

import { restartEngine } from '../api/supervisorClient';
import { QQ_NAPCAT } from '../platforms/registry';
import { useSettingsStore } from '../store/settingsStore';
import {
  Button,
  DangerPanel,
  Desc,
  Facts,
  Panel,
  ReadOnlyRow,
} from './components';
import { useLiveValue, useRuntime } from './consoleStore';
import { SchemaFields } from './FieldRenderer';
import { registerSectionComponents, type FieldSpec } from './fieldSchema';

const SECTION = 'link';

const QueueFacts = () => {
  const runtime = useRuntime();
  const wanted = useLiveValue<number>('queue_workers', 0);
  const enabled = useLiveValue<boolean>('enable_queue', false);
  const running = runtime.queue_workers_running ?? 0;
  const pending = runtime.volatile?.queue_pending ?? 0;
  if (!enabled) return null;
  return (
    <Facts>
      实际在跑 {running} 个 worker{running !== wanted && '，正在向配置对齐'}，队列里积压 {pending} 条。
    </Facts>
  );
};

const HistoryFacts = () => {
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

const BridgeFacts = () => {
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

const Environment = () => {
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

const RestartEngine = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [armed, setArmed] = useState(false);
  const [said, setSaid] = useState('');

  const fire = async () => {
    if (!token) return;
    setSaid('正在请求…');
    try {
      const result = await restartEngine(token, '用户从控制台请求重启引擎');
      setSaid(result?.scheduled ? '已排上重启，几秒后 worker 会重新起来。' : '调度器拒绝了这次重启。');
    } catch (error) {
      setSaid(error instanceof Error ? error.message : '请求失败');
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
      {said && <Facts className="basis-full">{said}</Facts>}
    </DangerPanel>
  );
};

registerSectionComponents(QQ_NAPCAT.id, SECTION, {
  queueFacts: QueueFacts,
  historyFacts: HistoryFacts,
  bridgeFacts: BridgeFacts,
  environment: Environment,
  restartEngine: RestartEngine,
});

const SCHEMA: FieldSpec[] = [
  {
    kind: 'block',
    title: '消息队列',
    hint: '开着的时候群消息排队处理，不再并发',
    children: [
      {
        kind: 'grid',
        children: [
          {
            kind: 'bool',
            key: 'enable_queue',
            label: '启用队列',
            desc: '按顺序一条条处理，避免同时开好几个对话。',
            children: [
              { kind: 'number', key: 'queue_workers', label: 'worker', unit: '个' },
              { kind: 'number', key: 'queue_interval', label: '间隔', step: 0.5, unit: '秒' },
            ],
          },
          {
            kind: 'bool',
            key: 'queue_wait_for_reply',
            label: '等回复',
            desc: '等上一条真的回完再取下一条，而不是发出去就算完。',
            children: [{ kind: 'number', key: 'queue_reply_timeout', label: '超时', step: 10, unit: '秒' }],
          },
        ],
      },
      { kind: 'custom', name: 'queueFacts' },
    ],
  },
  {
    kind: 'block',
    title: '群历史',
    hint: '给模型看的上下文，不是 QQ 的聊天记录',
    children: [
      {
        kind: 'grid',
        children: [
          {
            kind: 'fixed',
            label: '缓存条数',
            children: [
              { kind: 'desc', text: '每个群留多少条最近消息。0 表示不缓存。' },
              { kind: 'number', key: 'group_history_max', label: '每群', unit: '条' },
            ],
          },
          {
            kind: 'fixed',
            label: '未送达余量',
            children: [
              {
                kind: 'desc',
                text: '还没送到 Engine 的行可以多占几倍位置，硬上限 = 缓存条数 × 本值。1.0 表示不给余量，满了就丢。',
              },
              { kind: 'number', key: 'group_history_undelivered_ratio', label: '上限倍数', step: 0.5, unit: '倍' },
            ],
          },
        ],
      },
      { kind: 'custom', name: 'historyFacts' },
    ],
  },
  {
    kind: 'block',
    title: '转发桥',
    hint: '本地 HTTP 口子，供别的进程借 bot 发消息',
    children: [
      {
        kind: 'grid',
        children: [{
          kind: 'bool',
          key: 'enable_forward_bridge',
          label: '启用转发桥',
          desc: '关掉会立刻停掉这个本地端口。',
          fallback: true,
          children: [{ kind: 'number', key: 'forward_bridge_max_messages', label: '单次最多', unit: '条' }],
        }],
      },
      { kind: 'custom', name: 'bridgeFacts' },
    ],
  },
  {
    kind: 'block',
    title: '固定配置',
    hint: '改这些要重启进程，控制台只读',
    children: [{ kind: 'custom', name: 'environment' }],
  },
  {
    kind: 'block',
    title: '危险操作',
    hint: '唯一会中断正在进行的工作的操作',
    children: [{ kind: 'custom', name: 'restartEngine' }],
  },
  {
    kind: 'note',
    text: '队列、群历史、转发桥这几项改完同样是热生效 —— bot 会在两秒内重建 worker、deque 与'
      + '监听端口，不需要重启。上面那个重启按钮是给 engine worker 卡住时用的。',
  },
];

export const RuntimePage = () => (
  <SchemaFields platformId={QQ_NAPCAT.id} sectionId={SECTION} spec={SCHEMA} />
);
