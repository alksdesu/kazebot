// 请求作废：组件卸载、范围变化、换登录、同一路被后发的请求顶掉，这四种情况下迟到的响应一律不落地。
import { useSettingsStore, type SettingsState } from '../store/settingsStore';

export class SupersededRequest extends Error {
  constructor() {
    super('请求已作废');
    this.name = 'SupersededRequest';
  }
}

export interface RequestTicket {
  /** 作废时中止。只交给读请求：写请求半路中止，服务器多半照样做完，页面却当它没发生。 */
  readonly signal: AbortSignal;
  isCurrent: () => boolean;
  /** 已作废就抛 SupersededRequest。 */
  check: () => void;
}

export interface RequestScopeOptions {
  /** 公开端点的请求跟登录无关，换登录不作废。 */
  anonymous?: boolean;
}

// 登录 = 令牌加上它有没有核验通过。按换了几次计代，不比值：A→B→A 之后令牌又对上了，但已经不是发请求时那次登录。
const sessionOf = (state: Pick<SettingsState, 'adminToken' | 'isAuthenticated'>): string => `${state.isAuthenticated ? 1 : 0}:${state.adminToken || ''}`;
let login = { session: sessionOf(useSettingsStore.getState()), generation: 0 };
function observe(session: string): number {
  if (session !== login.session) login = { session, generation: login.generation + 1 };
  return login.generation;
}
// 订阅接住两次读取之间来回换的登录；读取时再比一次，不依赖订阅回调的先后。
useSettingsStore.subscribe((state) => { observe(sessionOf(state)); });

export const loginGeneration = (): number => observe(sessionOf(useSettingsStore.getState()));

export const subscribeLogin = (notify: () => void): (() => void) => useSettingsStore.subscribe(notify);

const abort = (controller: AbortController): void => controller.abort(new SupersededRequest());

// generation 为 null 表示不看登录。
function ticket(signal: AbortSignal, generation: number | null): RequestTicket {
  const isCurrent = () => !signal.aborted && (generation === null || loginGeneration() === generation);
  return { signal, isCurrent, check: () => { if (!isCurrent()) throw new SupersededRequest(); } };
}

/** 发出之前就已作废的请求：发起它的那一版页面已经不在了。 */
export const STALE_TICKET: RequestTicket = (() => {
  const controller = new AbortController();
  abort(controller);
  return ticket(controller.signal, null);
})();

/** 一组请求共用的作废规则。不依赖 React，store 里也能用；组件里用 useRequestGuard。 */
export class RequestScope {
  private epoch = new AbortController();
  private readonly lanes = new Map<string, AbortController>();

  constructor(private readonly options: RequestScopeOptions = {}) {}

  /** 同一 lane 后发的顶掉先发的；不给 lane 的互不顶替，只随 reset 作废。 */
  begin(lane?: string): RequestTicket {
    const generation = this.options.anonymous ? null : loginGeneration();
    if (lane === undefined) return ticket(this.epoch.signal, generation);
    this.drop(lane);
    const controller = new AbortController();
    this.lanes.set(lane, controller);
    return ticket(controller.signal, generation);
  }

  /** 作废这一路还没回来的请求，比如写操作一开始，之前发出的读取就都过时了。 */
  drop(lane: string): void {
    const controller = this.lanes.get(lane);
    if (!controller) return;
    this.lanes.delete(lane);
    abort(controller);
  }

  reset(): void {
    abort(this.epoch);
    this.epoch = new AbortController();
    for (const controller of this.lanes.values()) abort(controller);
    this.lanes.clear();
  }
}
