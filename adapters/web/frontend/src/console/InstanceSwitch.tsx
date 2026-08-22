// 账号切换器。多开时几个号各是一套独立后端，共用域名、各挂一个路径前缀。
import { useEffect, useState } from 'react';

import { getInstances, instanceConsoleHref, MOUNT, type ConsoleInstance } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { Select } from './components';

export const InstanceSwitch = () => {
  const token = useSettingsStore((state) => state.adminToken);
  const [rows, setRows] = useState<ConsoleInstance[]>([]);

  useEffect(() => {
    if (!token) return;
    void getInstances(token).then(setRows).catch(() => setRows([]));
  }, [token]);

  // 单实例部署无处可切，清单读不到时同理：不显示好过显示一个点不动的下拉。
  if (rows.length < 2) return null;

  // 以后端判定为准：前端的 MOUNT 是从地址栏反推的，配置写歪时会对不上。
  const current = rows.find((row) => row.current)?.path ?? MOUNT;

  const go = (path: string) => {
    if (path !== current) window.location.assign(instanceConsoleHref(path));
  };

  return (
    <label className="flex items-center gap-2.5">
      <span className="flex-none text-xs text-[var(--duties-secondary)]">当前账号</span>
      <Select
        aria-label="切换账号"
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
