// 定时轮询：上一轮还没回来就跳过这一轮。不跳的话读得比间隔慢时，后发的会把先发的作废，页面一次也落不了地，请求还越堆越多。

type Job = () => Promise<unknown>;

/** 同一时间只让一个 job 在跑：上一个还没回来，新来的直接跳过。返回这次跑没跑。失败由 job 自己处理。 */
export function createLatch(): (job: Job) => boolean {
  let running = false;
  return (job) => {
    if (running) return false;
    running = true;
    let pending: Promise<unknown>;
    // 同步抛错也按失败收尾，闩照样放开，不会从此停摆。
    try { pending = job(); } catch (error) { pending = Promise.reject(error); }
    void pending.finally(() => { running = false; });
    return true;
  };
}

/** 每隔 intervalMs 跑一次 task；给了 first 就先马上跑它一次，和之后的轮询共用同一个闩。返回停止函数。 */
export function startPolling(task: Job, intervalMs: number, { first }: { first?: Job } = {}): () => void {
  const run = createLatch();
  if (first) run(first);
  const timer = window.setInterval(() => run(task), intervalMs);
  return () => window.clearInterval(timer);
}
