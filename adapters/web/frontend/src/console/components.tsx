// 控制台各页的控件出口。通用件出自 settingsControls，这里只加一件：
// 不碰平台配置的页所用的素壳。平台页的状态条与折叠分区在 PlatformPage。
import { type ReactNode } from 'react';

import { PageShell } from '../components/settings/pages/settingsControls';
import { useSettingsStore } from '../store/settingsStore';

export {
  Block,
  Button,
  Check,
  Chip,
  Chips,
  DangerPanel,
  Desc,
  Empty,
  ErrorText,
  Facts,
  Field,
  Fixed,
  Footnote,
  Grid,
  Input,
  Item,
  ItemTitle,
  LinkButton,
  List,
  Loose,
  Option,
  Pager,
  Panel,
  Pip,
  Pre,
  ReadOnlyRow,
  Row,
  SaveBar,
  Segmented,
  Select,
  Sub,
  Tag,
  Textarea,
} from '../components/settings/pages/settingsControls';

/** 不碰平台配置的页面：只有标题、说明和登录提示，不去拉任何平台的生效状态。 */
export const PlainPage = ({ children, eyebrow, note, title }: {
  children: ReactNode;
  eyebrow: string;
  note: string;
  title: string;
}) => {
  const isAuthenticated = useSettingsStore((state) => state.isAuthenticated);
  return (
    <PageShell authed={isAuthenticated} description={note} eyebrow={eyebrow} title={title}>
      {children}
    </PageShell>
  );
};
