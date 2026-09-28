#!/usr/bin/env bash
# 拉代码、重启、确认 engine 真的起来了。装到 /usr/local/lib/kazebot/deploy.sh。
#
# git 必须以 kazebot 身份跑：root 跑过一次 reset --hard，被改动的文件和新建的目录属主全变 root，
# 服务以 kazebot 身份再写不动它们，而 supervisor 照常活着，systemd 显示 active running ——
# 表面看不出任何异常。
set -euo pipefail

ROOT=/opt/kazebot
OWNER=kazebot
SERVICE=kazebot
BRANCH="${1:-origin/master}"
# supervisor 起 uvicorn、engine 等它应答再注册，整条链路二十几秒。定死一个 sleep
# 要么误报要么白等，所以轮询。
WAIT_SEC=90

if [[ $EUID -ne 0 ]]; then
  echo "要用 root 跑：systemctl 和 chown 都需要。" >&2
  exit 1
fi

# reset --hard 会连本地改动一起抹掉。目标提交不再跟踪的文件，本地改动就是实例自己的配置
# （配置整个退出 git 的那次升级，config/ 下改过的节点和 runtime 就是这样）：先留一份，
# 切过去之后放回原处，成为盖在仓库默认上面的那一份。其余改动照旧丢弃，备份留着备查。
BACKUP="$ROOT/data/deploy-backup/$(date +%Y%m%d-%H%M%S)"
PRESERVED=()

preserve_local_changes() {
  local path
  while IFS= read -r -d '' path; do
    [[ -f "$ROOT/$path" ]] || continue
    [[ -d "$BACKUP" ]] || sudo -u "$OWNER" mkdir -p "$BACKUP"
    (cd "$ROOT" && sudo -u "$OWNER" cp -p --parents -- "$path" "$BACKUP/")
    PRESERVED+=("$path")
  done < <(sudo -u "$OWNER" git -C "$ROOT" diff --name-only -z HEAD --)
  if ((${#PRESERVED[@]})); then
    echo "[deploy] 本地改过 ${#PRESERVED[@]} 个受跟踪的文件，已备份到 $BACKUP"
  fi
}

restore_untracked_changes() {
  local path
  # 空数组在老版本 bash 的 set -u 下会被当成未定义。
  for path in ${PRESERVED[@]+"${PRESERVED[@]}"}; do
    if sudo -u "$OWNER" git -C "$ROOT" cat-file -e "HEAD:$path" 2>/dev/null; then
      echo "[deploy] $path 在新版本里仍归仓库管，本地改动没有放回（备份在 $BACKUP/$path）" >&2
      continue
    fi
    if [[ -e "$ROOT/$path" ]]; then
      echo "[deploy] $path 已经存在，没有覆盖（备份在 $BACKUP/$path）" >&2
      continue
    fi
    sudo -u "$OWNER" mkdir -p "$(dirname "$ROOT/$path")"
    sudo -u "$OWNER" cp -p -- "$BACKUP/$path" "$ROOT/$path"
    echo "[deploy] 放回 $path：新版本不再跟踪它，本地改动成为实例自己的配置"
  done
}

echo "[deploy] 拉取 ${BRANCH}"
sudo -u "$OWNER" git -C "$ROOT" fetch --all --prune
preserve_local_changes
sudo -u "$OWNER" git -C "$ROOT" reset --hard "$BRANCH"
restore_untracked_changes
echo "[deploy] 现在是 $(git -C "$ROOT" log -1 --format='%h %s')"

# 跑的是 /usr/local/lib 里的副本，git 更新的是仓库里那份。拉完自己同步一次，
# 免得改了部署逻辑还得先记着手动装一遍才生效。
SELF=$(readlink -f "$0")
SRC=$(readlink -f "$ROOT/deploy/deploy.sh" 2>/dev/null || true)
if [[ -n "$SRC" && "$SELF" != "$SRC" ]] && ! cmp -s "$SRC" "$SELF"; then
  install -m 0755 -o root -g root "$SRC" "$SELF"
  echo "[deploy] 部署脚本自身有更新，换新版重跑"
  exec "$SELF" "$@"
fi

# 宿主清理以 root 跑 /usr/local/lib 里的副本；装过的话跟着仓库更新，免得修了清理逻辑却一直跑旧的。
HOST_CLEANUP=/usr/local/lib/kazebot/qq_cache.py
if [[ -f "$HOST_CLEANUP" ]] && ! cmp -s "$ROOT/adapters/onebot/qq_cache.py" "$HOST_CLEANUP"; then
  install -m 0644 -o root -g root "$ROOT/adapters/onebot/qq_cache.py" "$HOST_CLEANUP"
  echo "[deploy] 宿主清理脚本有更新，已同步"
fi

# 兜底：谁手敲过一条 root 命令都能在这里改回来。
STRAY=$(find "$ROOT" ! -user "$OWNER" -print -quit 2>/dev/null || true)
if [[ -n "$STRAY" ]]; then
  echo "[deploy] 有文件不属于 ${OWNER}，全部改回来"
  chown -R "$OWNER:$OWNER" "$ROOT"
fi

# dist 不进 git，服务器得自己构建。跳过这步的话后端更新了、控制台还是上一版，
# 而且没有任何迹象说明这件事。
FRONTEND="$ROOT/adapters/web/frontend"
if [[ -d "$FRONTEND/node_modules" ]]; then
  echo "[deploy] 构建前端"
  if ! git -C "$ROOT" diff --quiet HEAD@{1} HEAD -- adapters/web/frontend/package-lock.json 2>/dev/null; then
    echo "[deploy] 依赖有变动，先 npm ci"
    sudo -u "$OWNER" npm --prefix "$FRONTEND" ci
  fi
  sudo -u "$OWNER" npm --prefix "$FRONTEND" run build
else
  echo "[deploy] 前端没装依赖，跳过构建（控制台会停在上一版）" >&2
fi

echo "[deploy] 重启 ${SERVICE}"
STARTED_AT=$(date +%s)
systemctl restart "$SERVICE"

# systemd 只看得到 supervisor。engine 是它 spawn 的，崩了这里照样 active running。
echo -n "[deploy] 等 engine worker"
for ((i = 0; i < WAIT_SEC; i += 2)); do
  if pgrep -f "python -m engine" >/dev/null; then
    echo " —— ${i}s"
    break
  fi
  echo -n .
  sleep 2
done

if ! pgrep -f "python -m engine" >/dev/null; then
  echo
  echo "[deploy] engine worker 没起来 —— supervisor 活着也没人接任务" >&2
  # 挑这次重启之后建的那个：上一批的日志还在，按 mtime 取会拿到旧的。
  for log in "$ROOT"/data/logs/engine-*.log; do
    [[ -f "$log" && $(stat -c %Y "$log") -ge $STARTED_AT ]] || continue
    echo "--- $log ---" >&2
    tail -30 "$log" >&2
  done
  exit 1
fi

echo "[deploy] 好了："
pgrep -af "python -m engine|python -m supervisor.main|python bot.py"
