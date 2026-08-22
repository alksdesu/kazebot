// 账号切换器。多开时几个号各是一套独立后端，共用域名、各挂一个路径前缀。
import { useEffect, useState } from 'react';

import { getInstances, MOUNT, type ConsoleInstance } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';

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
    if (path === current) return;
    // 换的是后端不是页面，只能整页跳。带上 query 才能停在同一页而不是弹回第一页。
    window.location.assign(`${path}/web/${window.location.search}`);
  };

  return (
    <label className="qc-rail-acct">
      <span className="qc-rail-acct-cap">当前账号</span>
      <select
        aria-label="切换账号"
        className="qc-rail-acct-pick"
        onChange={(event) => go(event.target.value)}
        value={current}
      >
        {rows.map((row) => (
          <option key={row.uin} value={row.path}>{row.label}</option>
        ))}
      </select>
    </label>
  );
};
