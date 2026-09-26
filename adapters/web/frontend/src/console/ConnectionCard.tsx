// 平台链路的实况：适配进程活没活、它和 NapCat 通没通。断一段 bot 就不说话。
import { Block, List, Pip, Row } from './components';
import { useConsoleStore, useRuntime } from './consoleStore';

const Link = ({ name, state, detail }: { name: string; state: string; detail: string }) => (
  <div className="flex flex-wrap items-baseline gap-2.5">
    <span className="min-w-[var(--duties-label-min-width)] font-mono text-xs font-medium">{name}</span>
    <span className="font-mono text-xs">{state}</span>
    <span className="font-mono text-mini text-[var(--duties-tertiary)]">{detail}</span>
  </div>
);

export const ConnectionCard = () => {
  const live = useConsoleStore((state) => state.live);
  const runtime = useRuntime();

  const botAlive = Boolean(live?.published && !live.stale);
  const napcat = Boolean(runtime.onebot_connected);

  return (
    <Block hint="两段链路，断一段 bot 就不说话" title="连接">
      <List>
        <Row>
          <Pip tone={botAlive ? 'live' : 'halt'} />
          <Link
            detail={runtime.pid ? `pid ${runtime.pid}` : ''}
            name="QQ 适配进程"
            state={botAlive ? '在线' : live?.published ? '心跳中断' : '没在跑'}
          />
        </Row>
        <Row>
          <Pip tone={napcat ? 'live' : botAlive ? 'halt' : 'idle'} />
          <Link
            detail={botAlive ? '' : '适配进程没在跑，这一项无从判断'}
            name="NapCat"
            state={!botAlive ? '未知' : napcat ? '已连接' : '未连接'}
          />
        </Row>
      </List>
    </Block>
  );
};
