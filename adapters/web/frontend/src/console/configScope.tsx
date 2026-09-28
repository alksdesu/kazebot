// 控件写到哪一份配置：平台的配置文件，或不分平台的 config/features.yaml。
// 草稿 store、分区组件、schema 都按它分家，同一套控件两边都能用。
import { createContext, useContext, type ReactNode } from 'react';

import { QQ_NAPCAT } from '../platforms/registry';

export type ConfigScopeKind = 'platform' | 'features';

export interface ConfigScope {
  id: string;
  kind: ConfigScopeKind;
  /** kind 为 platform 时是哪个平台，features 时为空串。 */
  platformId: string;
}

export const FEATURES_SCOPE: ConfigScope = Object.freeze({ id: 'features', kind: 'features', platformId: '' });

// 同一个平台永远拿到同一个对象：Provider 的 value 换了引用，底下所有订阅方都会重渲染。
const platformScopes = new Map<string, ConfigScope>();

export function platformScope(platformId: string): ConfigScope {
  let scope = platformScopes.get(platformId);
  if (!scope) {
    scope = Object.freeze({ id: `platform:${platformId}`, kind: 'platform', platformId });
    platformScopes.set(platformId, scope);
  }
  return scope;
}

// 默认给 NapCat QQ：单独渲染一个分区时不必先套 Provider。
const ConfigScopeContext = createContext<ConfigScope>(platformScope(QQ_NAPCAT.id));

export const ConfigScopeProvider = ({ children, scope }: { children: ReactNode; scope: ConfigScope }) => (
  <ConfigScopeContext.Provider value={scope}>{children}</ConfigScopeContext.Provider>
);

export const useConfigScope = (): ConfigScope => useContext(ConfigScopeContext);
