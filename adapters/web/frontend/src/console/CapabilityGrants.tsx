// 每项管理能力放到哪一档。能力清单由 bot 公布，这里只负责显示和写回。
import type { PlatformCapability } from '../api/supervisorClient';
import { Desc, Facts, Item, ItemTitle, List, Segmented } from './components';
import { useCapabilities, useConsoleStore, useLiveValue } from './consoleStore';

// 档位是固定的四个枚举，不像能力那样会增长，所以说法留在前端。
// 「加」表示在前一档基础上累加 —— 选了群主，名单里的人一样还能用。
const GRANT_LABELS: Record<string, string> = {
  off: '不开放',
  admin: '仅管理员',
  owner: '加群主',
  group_admin: '加群管',
};

const GROUP_ROLE_GRANTS = new Set(['owner', 'group_admin']);

const CapabilityRow = ({ cap }: { cap: PlatformCapability }) => {
  const setDraft = useConsoleStore((state) => state.setDraft);
  const configKey = `grant_${cap.key}`;
  const grant = useLiveValue<string>(configKey, cap.default);
  // 「本群」只在群角色真拿到这项能力时才有话说，那时它是在讲这项授权的边界。
  const scoped = cap.group_scoped && GROUP_ROLE_GRANTS.has(grant);

  return (
    <Item>
      <div className="flex items-center gap-2">
        <ItemTitle>{cap.name}</ItemTitle>
        {scoped && (
          <span className="border border-[var(--duties-live)] px-1.5 text-mini text-[var(--duties-live)]">
            只在本群
          </span>
        )}
        <span className="flex-1" />
        <Segmented
          choices={cap.grants.map((option) => [option, GRANT_LABELS[option] || option] as const)}
          onPick={(option) => setDraft(configKey, option)}
          value={grant}
        />
      </div>
      <Desc indent={false}>{cap.desc}</Desc>
    </Item>
  );
};

export const Capabilities = () => {
  const caps = useCapabilities();
  if (!caps.length) {
    return <Facts>bot 还没公布能力清单，连上之后这里才会有内容。</Facts>;
  }
  return (
    <List>
      {caps.map((cap) => (
        <CapabilityRow cap={cap} key={cap.key} />
      ))}
    </List>
  );
};
