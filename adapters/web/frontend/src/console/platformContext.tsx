// 当前页属于哪个平台。分区里的控件（号码表、备注、试听）都要知道，一层层传 prop 会穿过十几个组件。
import { createContext, useContext, type ReactNode } from 'react';

import { QQ_NAPCAT, type PlatformDescriptor } from '../platforms/registry';
import { ConfigScopeProvider, platformScope } from './configScope';

// 默认值给 NapCat QQ：单独渲染一个分区时不必先套 Provider。
const PlatformContext = createContext<PlatformDescriptor>(QQ_NAPCAT);

/** 平台页的控件同时认平台和配置作用域：一个管号码与备注，一个管草稿写进哪份文件。 */
export const PlatformProvider = ({ children, platform }: { children: ReactNode; platform: PlatformDescriptor }) => (
  <PlatformContext.Provider value={platform}>
    <ConfigScopeProvider scope={platformScope(platform.id)}>{children}</ConfigScopeProvider>
  </PlatformContext.Provider>
);

export const usePlatform = (): PlatformDescriptor => useContext(PlatformContext);
