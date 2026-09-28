// 平台链路的实况：适配进程活没活、它和平台那头通没通。断一段 bot 就不说话。
import { connectionPeerName } from '../platforms/registry';
import { Block, List, Pip, Row } from './components';
import { useConsoleStore, useLinkFacts, useRuntime } from './consoleStore';
import { nameWith } from './format';
import { usePlatform } from './platformContext';

const Link = ({ name, state, detail }: { name: string; state: string; detail: string }) => (
  <div className="flex flex-wrap items-baseline gap-2.5">
    <span className="min-w-[var(--duties-label-min-width)] font-mono text-xs font-medium">{name}</span>
    <span className="font-mono text-xs">{state}</span>
    <span className="font-mono text-mini text-[var(--duties-tertiary)]">{detail}</span>
  </div>
);

export const ConnectionCard = () => {
  const platform = usePlatform();
  const live = useConsoleStore((state) => state.live);
  const runtime = useRuntime();
  const link = useLinkFacts();

  const botAlive = Boolean(live?.published && !live.stale);
  const botState = botAlive ? '在线' : live?.published ? '心跳中断' : '没在跑';
  const linked = link.connected;
  const peerDetail = !botAlive ? `适配进程${botState}，这一项无从判断`
    : !linked ? platform.connectionHint
      : link.account ? `账号 ${link.account}` : '';

  return (
    <Block hint="两段链路，断一段 bot 就不说话" title="连接">
      <List>
        <Row>
          <Pip tone={botAlive ? 'live' : 'halt'} />
          <Link
            detail={runtime.pid ? `pid ${runtime.pid}` : ''}
            name={nameWith(platform.shortName, '适配进程')}
            state={botState}
          />
        </Row>
        <Row>
          <Pip tone={linked ? 'live' : botAlive ? 'halt' : 'idle'} />
          <Link
            detail={peerDetail}
            name={connectionPeerName(platform)}
            state={!botAlive ? '未知' : linked ? '已连接' : '未连接'}
          />
        </Row>
      </List>
    </Block>
  );
};
