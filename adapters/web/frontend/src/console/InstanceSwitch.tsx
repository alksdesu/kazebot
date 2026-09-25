// 账号切换器。多开时几个号各是一套独立后端，共用域名、各挂一个路径前缀。
import type { ComponentProps } from 'react';

import { instanceConsoleHref, MOUNT, type ConsoleInstance } from '../api/supervisorClient';
import { LinkButton, Select } from './components';

export const InstanceLink = ({ path, ...props }: Omit<ComponentProps<typeof LinkButton>, 'href'> & { path: string }) => (
  <LinkButton {...props} href={instanceConsoleHref(path)} />
);

export const InstanceSwitch = ({ rows }: { rows: ConsoleInstance[] }) => {
  // 单实例部署无处可切，清单读不到时同理：不显示好过显示一个点不动的下拉。
  if (rows.length < 2) return null;

  // 以后端判定为准：前端的 MOUNT 是从地址栏反推的，配置写歪时会对不上。
  const current = rows.find((row) => row.current)?.path ?? MOUNT;

  const go = (path: string) => {
    if (path !== current && rows.some(row => row.path === path)) {
      window.location.assign(instanceConsoleHref(path));
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
        {rows.map((row) => (
          <option key={row.uin} value={row.path}>{row.label}</option>
        ))}
      </Select>
    </label>
  );
};
