// 名单为空时的警告。两份名单空的后果不一样：群名单空等于哪个群都不说话，管理员名单空等于管理命令全停。
import { ErrorText } from './components';
import { useIdList, useNote } from './consoleStore';

const EmptyListWarning = ({ configKey, fallback }: { configKey: string; fallback: string }) => {
  const ids = useIdList(configKey);
  // bot 公布的键说明比这里写死的一句更准，优先用它。
  const note = useNote(configKey);
  if (ids.length) return null;
  return <ErrorText>{note || fallback}</ErrorText>;
};

export const GroupListWarning = () => (
  <EmptyListWarning configKey="allowed_groups" fallback="名单为空，bot 不会在任何群说话。" />
);

export const AdminListWarning = () => (
  <EmptyListWarning configKey="admin_users" fallback="名单为空，管理命令全部不可用。" />
);
