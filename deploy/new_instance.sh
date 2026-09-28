#!/usr/bin/env bash
# 命令行建实例。控制台的「多开实例」走的是同一条路，这里给的是控制台不可用时的手动入口。
#
# 用法: sudo deploy/new_instance.sh <实例名> [标签] [QQ号]
# 序号与端口自动分配，不用自己数。实例名是纯数字（老习惯按 QQ 号开）又没给 QQ 号时，它就是这个实例的 QQ 号。
set -euo pipefail

REPO="${KAZEBOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_ROOT="${KAZEBOT_DATA_ROOT:-/opt/kazebot-data}"
OWNER="${KAZEBOT_USER:-kazebot}"

[ "$(id -u)" -eq 0 ] || { echo "要 root" >&2; exit 1; }
INSTANCE="${1:-}"
LABEL="${2:-}"
QQ="${3:-}"
[[ "$INSTANCE" =~ ^([a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10})$ ]] || { echo "用法: $0 <实例名> [标签] [QQ号]" >&2; exit 2; }
# 数据根下的共享表情包库占着这个名字。
[ "$INSTANCE" != stickers ] || { echo "实例名不能叫 stickers" >&2; exit 2; }
if [[ -z "$QQ" && "$INSTANCE" =~ ^[0-9]+$ ]]; then QQ="$INSTANCE"; fi
[[ -z "$QQ" || "$QQ" =~ ^[0-9]+$ ]] || { echo "QQ 号必须是数字" >&2; exit 2; }
[ -x /usr/local/lib/kazebot/provision_instance.sh ] \
  || { echo "先跑一次 deploy/install_provision.sh" >&2; exit 1; }

# 工作区必须由 kazebot 建，root 建出来的文件它自己读不了。
# 脚手架逻辑只有 supervisor.provisioning 一份，这里不重写一遍。
echo "==> 建工作区"
sudo -u "$OWNER" CLONOTH_INSTANCES_FILE="$DATA_ROOT/instances.yaml" \
  "$REPO/.venv/bin/python" - "$REPO" "$INSTANCE" "$LABEL" "$QQ" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from platforms.builtin import QQ_NAPCAT
from supervisor.provisioning import plan_instance, scaffold

repo = Path(sys.argv[1])
plan = plan_instance(repo, instance_id=sys.argv[2], label=sys.argv[3], platform=QQ_NAPCAT, account=sys.argv[4])
scaffold(plan, repo)
print(f"    序号 {plan.idx}，supervisor {plan.ports['supervisor']}，工作区 {plan.workspace}")
PY

echo "==> 交给 root 侧（docker / systemd / cloudflared）"
systemctl start "kazebot-provision@$INSTANCE"

echo
echo "完事了。进度："
echo "  tail -f $DATA_ROOT/provision-$INSTANCE.log"
