// 组件里的请求守卫：卸载、范围变化、换登录时作废全部在途请求，同一 lane 的后发请求顶掉先发的。
import { useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';

import {
  loginGeneration,
  RequestScope,
  STALE_TICKET,
  subscribeLogin,
  SupersededRequest,
  type RequestScopeOptions,
  type RequestTicket,
} from '../api/requestGuard';

export interface RequestGuard {
  /** 范围加登录代次。拿去当 effect 依赖，或者给读回来的数据打上标签。 */
  readonly key: string;
  begin: (lane?: string) => RequestTicket;
  drop: (lane: string) => void;
  /** 渲染出这个守卫的那一版页面还在：范围没换、没换登录、没卸载。 */
  isCurrent: () => boolean;
  check: () => void;
}

const ignoreLogin = (): (() => void) => () => {};
const noLogin = (): number => 0;

/** scope 每换一次就换一代，换回原值也不认旧的；anonymous 只在第一次渲染时读。 */
export function useRequestGuard(scope: unknown = '', options: RequestScopeOptions = {}): RequestGuard {
  const [anonymous] = useState(() => Boolean(options.anonymous));
  const [requests] = useState(() => new RequestScope({ anonymous }));
  const generation = useSyncExternalStore(anonymous ? ignoreLogin : subscribeLogin, anonymous ? noLogin : loginGeneration);
  const key = JSON.stringify(anonymous ? [scope] : [scope, generation]);
  const committed = useRef<RequestGuard | null>(null);
  // 守卫对象本身就是那一代的凭证：范围从 A 换到 B 再换回 A，旧的那一个也不会再生效。
  const guard = useMemo<RequestGuard>(() => {
    const isCurrent = () => committed.current === current && (anonymous || loginGeneration() === generation);
    const current: RequestGuard = {
      key,
      begin: (lane) => (isCurrent() ? requests.begin(lane) : STALE_TICKET),
      drop: (lane) => { if (isCurrent()) requests.drop(lane); },
      isCurrent,
      check: () => { if (!isCurrent()) throw new SupersededRequest(); },
    };
    return current;
  }, [requests, key, anonymous, generation]);
  // 提交之后才换代：渲染到一半被丢弃的那一版，不该作废眼前页面的请求。
  useLayoutEffect(() => {
    committed.current = guard;
    return () => {
      committed.current = null;
      requests.reset();
    };
  }, [requests, guard]);
  return guard;
}
