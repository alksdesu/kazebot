#!/usr/bin/env bash
# 建一个 QQ 号实例的工作区：代码软链过来，数据与配置各自一份。
#
# 用法: deploy/new_instance.sh <uin> <序号> [标签]
#   序号 0 用现有端口与根路径，与单实例部署完全一致；每加一号序号 +1。
set -euo pipefail

# 从脚本自身位置推导，部署目录叫什么名字都不用改这里。
REPO="${KAZEBOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ROOT="${KAZEBOT_DATA_ROOT:-${REPO}-data}"
OWNER="${KAZEBOT_USER:-kazebot}"
# 工作区里这几个名字必须解析到代码：它们被 workspace_root 直接拼接。
LINKED_DIRS=(adapters engine plugins tools)

die() { echo "错误: $*" >&2; exit 1; }

[ $# -ge 2 ] || die "用法: $0 <uin> <序号> [标签]"
UIN="$1"
IDX="$2"
LABEL="${3:-$UIN}"

[[ "$UIN" =~ ^[0-9]+$ ]] || die "uin 必须是纯数字，收到 '$UIN'"
[[ "$IDX" =~ ^[0-9]+$ ]] || die "序号必须是非负整数，收到 '$IDX'"
[ -d "$REPO" ] || die "代码目录不存在: $REPO"

# 序号 0 落在现有端口上，升级单实例部署时不必改任何东西。
PORT=$((8765 + 10 * IDX))
BOT_PORT=$((8080 + 10 * IDX))
BRIDGE_PORT=$((8769 + 10 * IDX))
NAPCAT_PORT=$((6099 + 100 * IDX))
PREFIX=""
[ "$IDX" -gt 0 ] && PREFIX="i/$UIN"

WS="$ROOT/$UIN"
echo "==> 工作区 $WS (序号 $IDX)"
mkdir -p "$WS/data" "$WS/config"

for name in "${LINKED_DIRS[@]}"; do
  target="$REPO/$name"
  [ -d "$target" ] || die "代码目录缺少 $name"
  link="$WS/$name"
  # 已经是指向别处的链接就换掉；是真目录则停手，那多半是有人放了数据进去。
  if [ -L "$link" ]; then
    [ "$(readlink -f "$link")" = "$(readlink -f "$target")" ] || ln -sfn "$target" "$link"
  elif [ -e "$link" ]; then
    die "$link 是真实目录，不敢覆盖"
  else
    ln -s "$target" "$link"
  fi
done
echo "    已软链: ${LINKED_DIRS[*]}"

ENV_FILE="$WS/.env"
if [ -e "$ENV_FILE" ]; then
  echo "    .env 已存在，保持不动"
else
  cat > "$ENV_FILE" <<EOF
# 实例 $UIN ($LABEL)
CLONOTH_PORT=$PORT
HOST=127.0.0.1
PORT=$BOT_PORT
ONEBOT_FORWARD_BRIDGE_PORT=$BRIDGE_PORT
NAPCAT_WEBUI_URL=http://127.0.0.1:$NAPCAT_PORT
NAPCAT_WEBUI_TOKEN=
EOF
  [ -n "$PREFIX" ] && echo "CLONOTH_URL_PREFIX=$PREFIX" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "    已写 .env（NAPCAT_WEBUI_TOKEN 还是空的）"
fi

# 清单是几个实例共读的一份，用 yaml 库改而不是拼字符串。
"$REPO/.venv/bin/python" - "$ROOT/instances.yaml" "$UIN" "$LABEL" "$PREFIX" <<'PY'
import sys
from pathlib import Path

import yaml

path, uin, label, prefix = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
try:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
except OSError:
    doc = {}
rows = doc.get("instances") if isinstance(doc, dict) else None
rows = rows if isinstance(rows, list) else []
rows = [r for r in rows if not (isinstance(r, dict) and str(r.get("uin")) == uin)]
rows.append({"uin": uin, "label": label, "path": prefix})
rows.sort(key=lambda r: str(r.get("path") or ""))
path.write_text(yaml.safe_dump({"instances": rows}, allow_unicode=True, sort_keys=False), encoding="utf-8")
print(f"    清单已更新: {path}（共 {len(rows)} 个号）")
PY

if id "$OWNER" >/dev/null 2>&1; then
  chown -R "$OWNER:$OWNER" "$WS"
  chown "$OWNER:$OWNER" "$ROOT/instances.yaml"
fi

cat <<EOF

工作区就绪。剩下三步需要 root：

1) NapCat 容器（序号 0 用的是现有容器，不要重建）
   docker run -d --name napcat-$UIN --restart always \\
     -p 127.0.0.1:$NAPCAT_PORT:6099 \\
     -e NAPCAT_UID=0 -e NAPCAT_GID=0 -e TZ=Asia/Shanghai \\
     -v /opt/napcat-$UIN/config:/app/napcat/config \\
     -v /opt/napcat-$UIN/qq:/app/.config/QQ \\
     -v $WS/data/attachments:$WS/data/attachments \\
     mlikiowa/napcat-docker:v4.18.5
   反向 WS 指向 ws://host.docker.internal:$BOT_PORT/onebot/v11/ws
   再把 WebUI token 填进 $WS/.env 的 NAPCAT_WEBUI_TOKEN

2) 服务
   cp $REPO/deploy/systemd/kazebot@.service $REPO/deploy/systemd/kazebot-qq@.service \\
      $REPO/deploy/systemd/kazebot-cleanup@.service $REPO/deploy/systemd/kazebot-cleanup@.timer \\
      /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now kazebot@$UIN kazebot-qq@$UIN kazebot-cleanup@$UIN.timer

3) 入口
   序号 0 走原有域名；序号 >0 需要在 cloudflared 里把 /$PREFIX/ 指到 127.0.0.1:$PORT
EOF
