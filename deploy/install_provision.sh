#!/usr/bin/env bash
# 一次性安装：把 root 侧的建实例组件放到 kazebot 写不到的地方，并让主实例认得清单。
#
# 用法: sudo deploy/install_provision.sh <主实例名> [主实例QQ号]
# 主实例名写成 QQ 号（老习惯）又没另给 QQ 号时，它就是主实例的 QQ 号。
# 改过 provision_instance.sh / cloudflared_ingress.py / polkit 规则 / systemd 模板后要重跑一遍 ——
# git 更新的是 /opt/kazebot 里的源，不会自动同步到 /usr/local/lib 和 /etc。
set -euo pipefail

REPO="${KAZEBOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_ROOT="${KAZEBOT_DATA_ROOT:-/opt/kazebot-data}"
LIB=/usr/local/lib/kazebot
OWNER=kazebot

[ "$(id -u)" -eq 0 ] || { echo "要 root" >&2; exit 1; }
MAIN="${1:-}"
MAIN_QQ="${2:-}"
[[ "$MAIN" =~ ^([a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10})$ ]] || { echo "用法: $0 <主实例名> [主实例QQ号]" >&2; exit 2; }
# 数据根下的共享表情包库占着这个名字。
[ "$MAIN" != stickers ] || { echo "实例名不能叫 stickers" >&2; exit 2; }
if [[ -z "$MAIN_QQ" && "$MAIN" =~ ^[0-9]+$ ]]; then MAIN_QQ="$MAIN"; fi
[[ -z "$MAIN_QQ" || "$MAIN_QQ" =~ ^[0-9]+$ ]] || { echo "QQ 号必须是数字" >&2; exit 2; }

echo "==> 安装 root 侧组件到 $LIB"
install -d -m 0755 -o root -g root "$LIB" "$LIB/units"
install -m 0755 -o root -g root "$REPO/deploy/provision_instance.sh" "$LIB/provision_instance.sh"
install -m 0755 -o root -g root "$REPO/deploy/cloudflared_ingress.py" "$LIB/cloudflared_ingress.py"
# 这一份不用重装：它每次跑都会先把自己同步成仓库里的最新版。
install -m 0755 -o root -g root "$REPO/deploy/deploy.sh" "$LIB/deploy.sh"
install -m 0644 -o root -g root \
  "$REPO/deploy/systemd/kazebot@.service" \
  "$REPO/deploy/systemd/kazebot-qq@.service" \
  "$REPO/deploy/systemd/kazebot-cleanup@.service" \
  "$REPO/deploy/systemd/kazebot-cleanup@.timer" \
  "$LIB/units/"

echo "==> 装触发单元与 polkit 规则"
install -m 0644 -o root -g root \
  "$REPO/deploy/systemd/kazebot-provision@.service" \
  "$REPO/deploy/systemd/kazebot-deprovision@.service" \
  /etc/systemd/system/
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 -o root -g root \
  "$REPO/deploy/polkit/49-kazebot-provision.rules" /etc/polkit-1/rules.d/

echo "==> 准备 $DATA_ROOT"
install -d -m 0755 -o "$OWNER" -g "$OWNER" "$DATA_ROOT"

echo "==> 表情包库软链到共享目录"
install -d -m 0755 -o "$OWNER" -g "$OWNER" "$DATA_ROOT/stickers"
# 附件白名单只认工作区内的 data/ 前缀，所以是软链过去而不是配一个绝对路径。
if [ -L "$REPO/data/stickers" ]; then
  ln -sfn "$DATA_ROOT/stickers" "$REPO/data/stickers"
elif [ -e "$REPO/data/stickers" ]; then
  echo "    $REPO/data/stickers 已是真实目录，跳过；要共用的话自己把内容并过去再建软链"
else
  install -d -m 0755 -o "$OWNER" -g "$OWNER" "$REPO/data"
  ln -s "$DATA_ROOT/stickers" "$REPO/data/stickers"
  chown -h "$OWNER:$OWNER" "$REPO/data/stickers"
fi

echo "==> 放开主实例对 $DATA_ROOT 的写权限（drop-in，不动原 unit）"
install -d -m 0755 /etc/systemd/system/kazebot.service.d
cat >/etc/systemd/system/kazebot.service.d/10-instances.conf <<EOF
[Service]
ReadWritePaths=$DATA_ROOT
Environment=CLONOTH_INSTANCES_FILE=$DATA_ROOT/instances.yaml
EOF

# bot 进程也要能开表情包库。工作区里那个 data/stickers 只是软链，
# 而 systemd 按真实路径判权限，不放开共享目录它就打不开库文件。
install -d -m 0755 /etc/systemd/system/kazebot-qq.service.d
cat >/etc/systemd/system/kazebot-qq.service.d/10-stickers.conf <<EOF
[Service]
ReadWritePaths=$DATA_ROOT/stickers
EOF

systemctl daemon-reload

echo "==> 把主实例登记进清单"
sudo -u "$OWNER" CLONOTH_INSTANCES_FILE="$DATA_ROOT/instances.yaml" \
  "$REPO/.venv/bin/python" - "$REPO" "$MAIN" "$MAIN_QQ" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from platforms.builtin import QQ_NAPCAT
from supervisor.instances import save_instance
from supervisor.provisioning import normalize_accounts, validate_instance_id

instance_id = validate_instance_id(sys.argv[2])
accounts = normalize_accounts({QQ_NAPCAT.id: sys.argv[3]} if sys.argv[3] else None)
rows = save_instance(
    Path(sys.argv[1]), instance_id=instance_id, label=f"主实例 {instance_id}", idx=0, accounts=accounts,
)
print(f"    清单共 {len(rows)} 个实例")
PY

echo "==> 重启主实例让 drop-in 生效"
systemctl restart kazebot

cat <<EOF

装好了。现在控制台的「多开实例」里就能直接建实例。

自检：
  systemctl show kazebot -p ReadWritePaths | tr ' ' '\n' | grep kazebot-data
  sudo -u $OWNER systemctl start kazebot-provision@$MAIN   # 应当被 polkit 放行（会报工作区已存在）
  sudo -u $OWNER systemctl start sshd                          # 应当被拒绝
EOF
