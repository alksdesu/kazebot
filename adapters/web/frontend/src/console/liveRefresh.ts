// 生效状态不会推到浏览器，只能拉：页面开着就跟着读，进页读一次，之后定时读，切回页面再读一次。
import { useEffect } from 'react';

// 状态条、连接卡、各平台换没换上都靠它跟上实况。刚写回那阵子认账那边两秒探一次，这里不必更勤。
const LIVE_REFRESH_MS = 15_000;
// 切回标签页时 focus 和 visibilitychange 前后脚都会来，读一次就够。
const RERUN_GAP_MS = 1000;

/** refresh 的引用要稳定，store 里的 action 就是。没登录时什么也不做；页面藏在后台时不读。 */
export function useLiveRefresh(refresh: (token: string) => Promise<void>, token: string | null | undefined): void {
  useEffect(() => {
    if (!token) return undefined;
    let last = Number.NEGATIVE_INFINITY;
    const run = () => {
      if (document.hidden) return;
      const now = Date.now();
      if (now - last < RERUN_GAP_MS) return;
      last = now;
      void refresh(token);
    };
    run();
    const timer = window.setInterval(run, LIVE_REFRESH_MS);
    window.addEventListener('focus', run);
    document.addEventListener('visibilitychange', run);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', run);
      document.removeEventListener('visibilitychange', run);
    };
  }, [refresh, token]);
}
