#!/usr/bin/env bash
# 拉代码、重启、确认 engine 真的起来了。装到 /usr/local/lib/kazebot/deploy.sh。
#
# git 必须以 kazebot 身份跑：root 跑过一次 reset --hard，被改动的文件属主全变 root，
# engine 启动要写 tools/__init__.py 就 EACCES，而 supervisor 照常活着，systemd 显示
# active running —— 表面看不出任何异常，实际没人接任务。
set -euo pipefail

ROOT=/opt/kazebot
OWNER=kazebot
SERVICE=kazebot
BRANCH="${1:-origin/master}"
SETTLE_SEC=15

if [[ $EUID -ne 0 ]]; then
  echo "要用 root 跑：systemctl 和 chown 都需要。" >&2
  exit 1
fi

echo "[deploy] 拉取 ${BRANCH}"
sudo -u "$OWNER" git -C "$ROOT" fetch --all --prune
sudo -u "$OWNER" git -C "$ROOT" reset --hard "$BRANCH"
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

# 兜底：谁手敲过一条 root 命令都能在这里改回来。
STRAY=$(find "$ROOT" ! -user "$OWNER" -print -quit 2>/dev/null || true)
if [[ -n "$STRAY" ]]; then
  echo "[deploy] 有文件不属于 ${OWNER}，全部改回来"
  chown -R "$OWNER:$OWNER" "$ROOT"
fi

echo "[deploy] 重启 ${SERVICE}"
systemctl restart "$SERVICE"
sleep "$SETTLE_SEC"

# systemd 只看得到 supervisor。engine 是它 fork 出来的，崩了这里照样 active。
if ! pgrep -af "python -m engine" >/dev/null; then
  echo "[deploy] engine worker 没起来 —— supervisor 活着也没人接任务" >&2
  tail -30 "$(ls -t "$ROOT"/data/logs/engine-*.log 2>/dev/null | head -1)" >&2 || true
  exit 1
fi

echo "[deploy] 好了："
pgrep -af "python -m engine|python -m supervisor.main|python bot.py"
