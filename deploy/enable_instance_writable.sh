#!/usr/bin/env bash
# 只由 root 侧 systemd 单元调用，给指定实例安装一次代码目录写权限。
set -euo pipefail

INSTANCE="${1:-}"
[[ "$INSTANCE" =~ ^([a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10})$ ]] || { echo "实例名不合法" >&2; exit 2; }
case "$INSTANCE" in stickers|shared) echo "实例名不合法" >&2; exit 2 ;; esac

UNIT="kazebot@${INSTANCE}.service"
DROPIN_DIR="/etc/systemd/system/${UNIT}.d"
DROPIN="$DROPIN_DIR/50-writable.conf"
TMP="${DROPIN}.tmp.$$"
trap 'rm -f "$TMP"' EXIT

systemctl cat "$UNIT" >/dev/null
install -d -m 0755 -o root -g root "$DROPIN_DIR"
cat >"$TMP" <<EOF
[Service]
ReadWritePaths=/opt/kazebot
NoNewPrivileges=no
RestrictSUIDSGID=no
EOF
chown root:root "$TMP"
chmod 0644 "$TMP"
mv -f "$TMP" "$DROPIN"

systemctl daemon-reload
sleep 1
systemctl restart "$UNIT"
systemctl is-active --quiet "$UNIT"
echo "代码目录写权限已启用：$UNIT"
