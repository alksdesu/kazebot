// 控制台 schema 的缓存。布局只随代码变，一个作用域拉一次就够；侧栏搜索和页面读的是同一份。
import { create } from 'zustand';

import { getFeaturesSchema, getPlatformSchema, type ConsoleSchema } from '../api/supervisorClient';
import type { ConfigScope } from './configScope';

interface SchemaState {
  schemas: Readonly<Record<string, ConsoleSchema>>;
  errors: Readonly<Record<string, string>>;
  load: (token: string, scope: ConfigScope) => Promise<void>;
}

// 同一个作用域并发的几次加载合成一个请求：侧栏和页面会在同一轮渲染里各要一次。
const inflight = new Map<string, Promise<void>>();

const fetchSchema = (token: string, scope: ConfigScope): Promise<ConsoleSchema> => (
  scope.kind === 'features' ? getFeaturesSchema(token) : getPlatformSchema(token, scope.platformId)
);

// 路由不存在时 FastAPI 回的就是这一句：supervisor 还是旧版，没有这个接口。
const ROUTE_MISSING = '404 Not Found';

const describe = (error: unknown): string => {
  const message = error instanceof Error ? error.message : String(error);
  if (message === ROUTE_MISSING) {
    return `读不到控制台布局（${message}）。supervisor 可能还没更新到当前版本，重启 supervisor 之后再试。`;
  }
  // 别的错误带着后端给的原因（比如这个平台没有控制台配置界面），照原话说，状态码不念。
  return `读不到控制台布局：${message.replace(/^\d{3} /, '')}`;
};

export const useSchemaStore = create<SchemaState>((set, get) => ({
  schemas: {},
  errors: {},

  load: (token, scope) => {
    if (get().schemas[scope.id]) return Promise.resolve();
    const running = inflight.get(scope.id);
    if (running) return running;
    const task = fetchSchema(token, scope)
      .then((schema) => {
        const { [scope.id]: _dropped, ...errors } = get().errors;
        set({ schemas: { ...get().schemas, [scope.id]: schema }, errors });
      })
      .catch((error: unknown) => {
        set({ errors: { ...get().errors, [scope.id]: describe(error) } });
      })
      .finally(() => { inflight.delete(scope.id); });
    inflight.set(scope.id, task);
    return task;
  },
}));

export interface SchemaSlot {
  schema: ConsoleSchema | null;
  error: string;
}

/** 某个作用域的 schema 和加载错误。没拉到之前 schema 为 null。 */
export function useConsoleSchema(scope: ConfigScope): SchemaSlot {
  const schema = useSchemaStore((state) => state.schemas[scope.id] || null);
  const error = useSchemaStore((state) => state.errors[scope.id] || '');
  return { schema, error };
}
