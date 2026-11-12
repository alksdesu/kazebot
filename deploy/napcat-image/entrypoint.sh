#!/bin/bash
# 容器入口：铺好 NapCat、尽量抹掉容器痕迹、以 napcat 身份在 Xvfb 下起 QQ。
trap "" SIGPIPE
cd /app

if [ ! -f napcat/napcat.mjs ]; then
  unzip -q NapCat.Shell.zip -d NapCat.Shell && cp -rf NapCat.Shell/* napcat/ && rm -rf NapCat.Shell
fi
if [ ! -f napcat/config/napcat.json ]; then
  unzip -q NapCat.Shell.zip -d NapCat.Shell && cp -rf NapCat.Shell/config/* napcat/config/ && rm -rf NapCat.Shell
fi

WEBUI_CONFIG=/app/napcat/config/webui.json
if [ ! -f "$WEBUI_CONFIG" ] && [ -n "${WEBUI_TOKEN:-}" ]; then
  cat > "$WEBUI_CONFIG" <<JSON
{
    "host": "0.0.0.0",
    "prefix": "${WEBUI_PREFIX:-}",
    "port": 6099,
    "token": "${WEBUI_TOKEN}",
    "loginRate": 3
}
JSON
fi

rm -rf /tmp/.X1-lock
rm -f /.dockerenv /.dockerinit /run/.containerenv /run/systemd/container /dev/.dockerenv
[ -S /var/run/docker.sock ] && mv /var/run/docker.sock /var/run/.docker.sock.hidden 2>/dev/null

# 下面的 bind mount 没有 CAP_SYS_ADMIN 会静默失败，容器照常起；有权限时 QQ 看到的就是一台普通主机。
FAKE=/tmp/fake_host
mkdir -p "$FAKE"
fake_file() { # 原路径 内容来源命令
  local target="$1"; shift
  [ -f "$target" ] || return 0
  local copy="$FAKE/$(printf '%s' "$target" | tr '/' '_')"
  "$@" > "$copy" 2>/dev/null && mount --bind "$copy" "$target" 2>/dev/null || true
}
scrub_cgroup() { sed -e 's|/docker/|/system.slice/|g' -e 's|/docker-|/system.slice/|g' -e 's|/lxc/|/system.slice/|g' \
  -e 's|/podman/|/system.slice/|g' -e 's|/kubepods/|/system.slice/|g' -e 's|/containerd/|/system.slice/|g' -e 's|/buildkit/|/system.slice/|g' "$1"; }
scrub_mounts() { sed -e 's|overlay / |ext4 / |g' -e 's|overlay /|ext4 /|g' -e '/docker/d' -e '/containerd/d' -e '/\.dockerenv/d' "$1"; }
for f in /proc/self/cgroup /proc/1/cgroup; do fake_file "$f" scrub_cgroup "$f"; done
for f in /proc/self/mountinfo /proc/1/mountinfo /proc/self/mounts; do fake_file "$f" scrub_mounts "$f"; done
fake_file /proc/self/cpuset sed -e 's|/docker/|/|g' -e 's|/docker-[^/]*/|/|g' -e 's|/kubepods/|/|g' -e 's|/containerd/|/|g' /proc/self/cpuset
fake_file /proc/1/cmdline printf '/sbin/init\0'
fake_file /proc/1/stat sed 's/^\([0-9]*\) ([-a-zA-Z0-9_]*)/\1 (systemd)/' /proc/1/stat
fake_file /proc/1/sched sh -c "head -1 /proc/1/sched | sed 's|([^)]*)|(1, #threads=1)|; s/^[a-zA-Z0-9_-]*\s/systemd /'; tail -n +2 /proc/1/sched"
fake_file /proc/1/environ sh -c "tr '\0' '\n' < /proc/1/environ | grep -viE 'container=(docker|lxc|podman|containerd)' | tr '\n' '\0'"
for f in /proc/1/status /proc/self/status; do fake_file "$f" sed 's/^NSpid:.*/NSpid:\t1/' "$f"; done
fake_file /sys/hypervisor/type echo ""
fake_file /sys/class/dmi/id/product_name echo "Standard PC"
fake_file /sys/class/dmi/id/product_uuid echo "00000000-0000-0000-0000-000000000000"
fake_file /sys/class/dmi/id/sys_vendor echo "Intel Corporation"
fake_file /sys/class/dmi/id/board_vendor echo "Intel Corporation"
fake_file /sys/class/dmi/id/board_name echo "Default string"
fake_file /etc/hosts sh -c "grep -v \"$(hostname)\" /etc/hosts; printf '127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost ip6-loopback\n'"

: "${NAPCAT_UID:=0}" "${NAPCAT_GID:=0}"
usermod -o -u "$NAPCAT_UID" napcat
groupmod -o -g "$NAPCAT_GID" napcat
usermod -g "$NAPCAT_GID" napcat
chown -R "$NAPCAT_UID:$NAPCAT_GID" /app

gosu napcat Xvfb :1 -screen 0 1080x760x16 +extension GLX +render > /dev/null 2>&1 &
sleep 2

export FFMPEG_PATH=/usr/bin/ffmpeg DISPLAY=:1
cd /app/napcat
if [ -n "${ACCOUNT:-}" ]; then
  exec gosu napcat /opt/QQ/qq --no-sandbox -q "$ACCOUNT"
fi
exec gosu napcat /opt/QQ/qq --no-sandbox
