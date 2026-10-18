// 实例切换器。多开时几个 bot 各是一套独立后端，共用域名、各挂一个路径前缀。
import type { ComponentProps } from 'react';

import { instanceConsoleHref, MOUNT, type ConsoleInstance } from '../api/supervisorClient';
import { clearInstanceToken, navigateToInstance } from '../instances/navigation';
import { isInstancePath } from '../instances/paths';
import { LinkButton, Select } from './components';

export const InstanceLink = ({ path, onClick, ...props }: Omit<ComponentProps<typeof LinkButton>, 'href'> & { path: string }) => isInstancePath(path) ? (
  <LinkButton {...props} href={instanceConsoleHref(path)} referrerPolicy="no-referrer" onClick={event => {
    onClick?.(event);
    if (event.defaultPrevented) return;
    clearInstanceToken();
  }} />
) : <span aria-disabled="true" title="实例地址无效">{props.children}</span>;

export const InstanceSwitch = ({ rows }: { rows: ConsoleInstance[] }) => {
  const navigable = rows.filter(row => isInstancePath(row.path));
  // 单实例部署无处可切，清单读不到时同理：不显示好过显示一个点不动的下拉。
  if (navigable.length < 2) return null;

  // 以后端判定为准：前端的 MOUNT 是从地址栏反推的，配置写歪时会对不上。
  const current = navigable.find((row) => row.current)?.path ?? MOUNT;

  const go = (path: string) => {
    if (path !== current && navigable.some(row => row.path === path)) {
      navigateToInstance(path);
    }
  };

  return (
    <label className="flex items-center gap-2.5">
      <span className="flex-none text-xs text-[var(--duties-secondary)]">当前实例</span>
      <Select
        aria-label="切换实例"
        onChange={(event) => go(event.target.value)}
        value={current}
        width="alias"
      >
        {navigable.map((row) => (
          <option key={row.id} value={row.path}>{row.label}</option>
        ))}
      </Select>
    </label>
  );
};
