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
# 数据根下的共享表情包库和共享配置组占着这两个名字。
case "$MAIN" in stickers|shared) echo "实例名不能叫 $MAIN" >&2; exit 2 ;; esac
if [[ -z "$MAIN_QQ" && "$MAIN" =~ ^[0-9]+$ ]]; then MAIN_QQ="$MAIN"; fi
[[ -z "$MAIN_QQ" || "$MAIN_QQ" =~ ^[0-9]+$ ]] || { echo "QQ 号必须是数字" >&2; exit 2; }

echo "==> 安装 root 侧组件到 $LIB"
install -d -m 0755 -o root -g root "$LIB" "$LIB/units"
install -m 0755 -o root -g root "$REPO/deploy/provision_instance.sh" "$LIB/provision_instance.sh"
install -m 0755 -o root -g root "$REPO/deploy/enable_instance_writable.sh" "$LIB/enable_instance_writable.sh"
install -m 0755 -o root -g root "$REPO/deploy/cloudflared_ingress.py" "$LIB/cloudflared_ingress.py"
# 这一份不用重装：它每次跑都会先把自己同步成仓库里的最新版。
install -m 0755 -o root -g root "$REPO/deploy/deploy.sh" "$LIB/deploy.sh"
# 宿主清理以 root 跑这一份；之后 deploy.sh 每次更新会顺带同步它。
install -m 0644 -o root -g root "$REPO/adapters/onebot/qq_cache.py" "$LIB/qq_cache.py"
install -m 0644 -o root -g root \
  "$REPO/deploy/systemd/kazebot@.service" \
  "$REPO/deploy/systemd/kazebot-qq@.service" \
  "$REPO/deploy/systemd/kazebot-cleanup@.service" \
  "$REPO/deploy/systemd/kazebot-cleanup@.timer" \
  "$LIB/units/"

echo "==> 装触发单元、宿主清理与 polkit 规则"
install -m 0644 -o root -g root \
  "$REPO/deploy/systemd/kazebot-provision@.service" \
  "$REPO/deploy/systemd/kazebot-deprovision@.service" \
  "$REPO/deploy/systemd/kazebot-writable@.service" \
  "$REPO/deploy/systemd/kazebot-host-cleanup.service" \
  "$REPO/deploy/systemd/kazebot-host-cleanup.timer" \
  /etc/systemd/system/
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 -o root -g root \
  "$REPO/deploy/polkit/49-kazebot-provision.rules" /etc/polkit-1/rules.d/

echo "==> 准备 $DATA_ROOT"
install -d -m 0755 -o "$OWNER" -g "$OWNER" "$DATA_ROOT"
# 共享配置组放这下面；组里有模型密钥，只给属主进。
install -d -m 0700 -o "$OWNER" -g "$OWNER" "$DATA_ROOT/shared"

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
# NapCat 容器里的缓存从此归宿主清理，engine 定时拉起的数据清理只管本工作区
Environment=CLONOTH_MAINTENANCE_SCOPE=workspace
EOF

# bot 进程也要能开表情包库。工作区里那个 data/stickers 只是软链，
# 而 systemd 按真实路径判权限，不放开共享目录它就打不开库文件。
install -d -m 0755 /etc/systemd/system/kazebot-qq.service.d
cat >/etc/systemd/system/kazebot-qq.service.d/10-stickers.conf <<EOF
[Service]
ReadWritePaths=$DATA_ROOT/stickers
EOF
# 主实例加入共享配置组后 config/ 是指到 $DATA_ROOT/shared 下的链接，bot 同步收藏表情时要往里写。
cat >/etc/systemd/system/kazebot-qq.service.d/20-shared-config.conf <<EOF
[Service]
ReadWritePaths=-$DATA_ROOT/shared
EOF

systemctl daemon-reload

echo "==> 启用宿主清理"
systemctl enable --now kazebot-host-cleanup.timer

echo "==> 把主实例登记进清单"
sudo -u "$OWNER" CLONOTH_INSTANCES_FILE="$DATA_ROOT/instances.yaml" \
  "$REPO/.venv/bin/python" - "$REPO" "$MAIN" "$MAIN_QQ" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from platforms.builtin import QQ_NAPCAT
from supervisor.instances import save_instance
from supervisor.provisioning import normalize_account, validate_instance_id

instance_id = validate_instance_id(sys.argv[2])
rows = save_instance(
    Path(sys.argv[1]),
    instance_id=instance_id,
    label=f"主实例 {instance_id}",
    idx=0,
    platform=QQ_NAPCAT.id,
    account=normalize_account(QQ_NAPCAT, sys.argv[3]),
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
  systemctl list-timers kazebot-host-cleanup.timer             # 应当排着下一次宿主清理
EOF
