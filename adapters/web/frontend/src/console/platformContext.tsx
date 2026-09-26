// 当前页属于哪个平台。分区里的控件（号码表、备注、试听）都要知道，一层层传 prop 会穿过十几个组件。
import { createContext, useContext, type ReactNode } from 'react';

import { QQ_NAPCAT, type PlatformDescriptor } from '../platforms/registry';

// 默认值给 NapCat QQ：单独渲染一个分区时不必先套 Provider。
const PlatformContext = createContext<PlatformDescriptor>(QQ_NAPCAT);

export const PlatformProvider = ({ children, platform }: { children: ReactNode; platform: PlatformDescriptor }) => (
  <PlatformContext.Provider value={platform}>{children}</PlatformContext.Provider>
);

export const usePlatform = (): PlatformDescriptor => useContext(PlatformContext);
