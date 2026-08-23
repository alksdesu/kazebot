#!/usr/bin/env bash
# 裸机一键部署：空服务器 → 控制台能打开。
#
# 用法: git clone <url> /opt/kazebot && sudo /opt/kazebot/deploy/bootstrap.sh
#
# 不问任何问题。模型渠道、管理员、群白名单、扫码登录全在控制台里填 ——
# 那边有校验、能即时生效、改错了还能改回来，比在命令行里敲一遍强。
# 只认全新机器；已经在跑的部署用 deploy.sh 更新。
set -euo pipefail

REPO=/opt/kazebot
DATA_ROOT=/opt/kazebot-data
NAPCAT_HOME=/opt/napcat
OWNER=kazebot
IMAGE="${KAZEBOT_NAPCAT_IMAGE:-mlikiowa/napcat-docker:v4.18.5}"

# 必须逐字等于 supervisor/instances.py 里 ports_for(0)，否则以后多开会重新分配端口
SUP_PORT=8765
BOT_PORT=8080
BRIDGE_PORT=8769
NAPCAT_PORT=6099

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
skip() { printf '    \033[2m· %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*" >&2; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

as_owner() { sudo -u "$OWNER" "$@"; }

# ---------------------------------------------------------------- 前置检查

preflight() {
  say "检查环境"
  [ "$(id -u)" -eq 0 ] || fail "要 root 跑：sudo $0"
  command -v systemctl >/dev/null 2>&1 || fail "需要 systemd"

  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  [ "$here" = "$REPO" ] || fail "仓库必须放在 $REPO（现在在 $here）。先 git clone 到 $REPO 再跑。"

  # 判据是「服务在跑」而不是「文件存在」：在跑着的号上重写配置等于砸场子，必须拒绝；
  # 但上次跑到一半失败的机器得能接着跑，每一步自己会跳过已完成的部分。
  if systemctl is-active --quiet kazebot 2>/dev/null; then
    fail "kazebot 正在运行，这是台现役机器。更新代码请用 deploy/deploy.sh"
  fi
  [ -f "$REPO/.env" ] && warn "上次没跑完 —— 已完成的步骤会跳过，现有配置一律不覆盖"

  info "仓库位置正确，可以开始"
}

# ---------------------------------------------------------------- 系统依赖

install_packages() {
  say "装系统依赖"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    python3 python3-venv python3-pip \
    git curl ca-certificates openssl >/dev/null

  local pyver
  pyver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "Python $pyver 太老，至少要 3.10（推荐 3.11+）"
  info "Python $pyver"

  # 控制台是配置和扫码的唯一入口，dist 编不出来这台机器就没法用 —— Node 不是可选项
  command -v npm >/dev/null 2>&1 || apt-get install -y -qq nodejs npm >/dev/null
  local nodever nodemajor
  nodever="$(node -v 2>/dev/null || echo v0)"
  nodemajor="${nodever#v}"; nodemajor="${nodemajor%%.*}"
  if [ "${nodemajor:-0}" -lt 18 ] 2>/dev/null; then
    warn "Node $nodever 过老（前端要 18+），构建会失败。装新版再重跑本脚本"
  fi
  info "Node $nodever"

  if ! command -v docker >/dev/null 2>&1; then
    info "装 docker…"
    curl -fsSL https://get.docker.com | sh >/dev/null
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  info "Docker $(docker --version | awk '{print $3}' | tr -d ,)"
}

# ---------------------------------------------------------------- 用户与目录

create_user_and_dirs() {
  say "建用户与目录"
  if id "$OWNER" >/dev/null 2>&1; then
    skip "用户 $OWNER 已存在"
  else
    useradd -r -s /usr/sbin/nologin -d "$REPO" "$OWNER"
    info "建了系统用户 $OWNER（nologin）"
  fi

  install -d -m 0755 -o "$OWNER" -g "$OWNER" "$DATA_ROOT"
  # 代码目录本身就是第一个号的工作区，所以它必须可写：engine 起来要 touch
  # tools/__init__.py，控制台改节点要写 config/nodes/*.yaml。多开时第二个号起才是只读代码。
  chown -R "$OWNER:$OWNER" "$REPO"
  install -d -m 0700 -o "$OWNER" -g "$OWNER" "$REPO/data"
  install -d -m 0755 -o "$OWNER" -g "$OWNER" "$REPO/data/attachments" "$REPO/data/logs"
  info "$REPO 与 $DATA_ROOT 属主已归 $OWNER"
}

setup_venv() {
  say "建 venv 装依赖"
  if [ -x "$REPO/.venv/bin/python" ]; then
    skip ".venv 已存在"
  else
    as_owner python3 -m venv "$REPO/.venv"
  fi
  as_owner "$REPO/.venv/bin/pip" install -q --upgrade pip
  as_owner "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"
  info "依赖装好了（含 nonebot2 与 onebot 适配器）"
}

# ---------------------------------------------------------------- 配置

write_env() {
  say "生成 .env"
  if [ -f "$REPO/.env" ]; then
    skip ".env 已存在，不覆盖"
    return
  fi
  local bridge_token hash_secret
  bridge_token="$(openssl rand -hex 24)"
  # 全新工作区才能随机生成：改这个值等于所有会话摘要键失配，存量部署必须走
  # deploy/migrate_qq_conversation_hash.py 迁移。
  hash_secret="$(openssl rand -hex 32)"

  # 这份只放「机器相关」的东西。模型渠道、管理员、群白名单都归控制台管，
  # 写在这里反而会跟控制台写的那份打架（config/qq.yaml 的优先级比 env 高）。
  # systemd 的 EnvironmentFile 不支持 export 前缀，别加。
  cat >"$REPO/.env" <<EOF
# 由 deploy/bootstrap.sh 生成。业务配置请在控制台里改，不要往这里加。

# ── Supervisor ────────────────────────────────
CLONOTH_HOST=127.0.0.1
CLONOTH_PORT=$SUP_PORT
CLONOTH_BASE_URL=http://127.0.0.1:$SUP_PORT
CLONOTH_ENTRY_NODE=qq.orchestrator
CLONOTH_LOG_LEVEL=info
# 仓库里没有 adapters/shell，开了只会刷启动错误
CLONOTH_SPAWN_SHELL_CLI=0

# 第一个号的工作区就是代码目录，CLONOTH_WORKSPACE 留空即回落到这里。
# 显式写错会让附件、记忆、admin token 静默落到两个地方。

# ── NoneBot ───────────────────────────────────
DRIVER=~fastapi
HOST=127.0.0.1
PORT=$BOT_PORT
LOG_LEVEL=INFO

# ── 转发 Bridge ───────────────────────────────
# 只听本机，但本机任何进程都能调（含 agent 自己的 execute_command），所以强制校验
ONEBOT_FORWARD_BRIDGE_PORT=$BRIDGE_PORT
ONEBOT_FORWARD_BRIDGE_TOKEN=$bridge_token

# ── 会话摘要盐 ────────────────────────────────
# 无盐的话摘要输入只有「前缀 + 十位数字」，枚举一遍就能反推真实群号
ONEBOT_CONVERSATION_HASH_SECRET=$hash_secret

# ── NapCat ────────────────────────────────────
NAPCAT_WEBUI_URL=http://127.0.0.1:$NAPCAT_PORT
# 容器首启后由本脚本回填，控制台扫码要用
NAPCAT_WEBUI_TOKEN=

# ── 数据保留 ──────────────────────────────────
CLONOTH_QQ_CACHE_MAX_AGE_SECONDS=604800
CLONOTH_MEMORY_ENTRY_MAX_AGE_SECONDS=1209600
EOF
  chown "$OWNER:$OWNER" "$REPO/.env"
  chmod 600 "$REPO/.env"
  info "Bridge token 与会话盐已随机生成"
}

write_configs() {
  say "铺配置模板"

  copy_template() { # copy_template <源> <目标> <说明>
    if [ -f "$2" ]; then
      skip "$(basename "$2") 已存在"
      return
    fi
    as_owner cp "$1" "$2"
    chmod 600 "$2"
    info "$3"
  }

  copy_template "$REPO/config.example.yaml" "$REPO/data/config.yaml" \
    "data/config.yaml —— 空渠道，等控制台「渠道」页填"
  # 比内置默认更严：额外禁读 config/nodes/** 与 engine/system_nodes/**
  copy_template "$REPO/policy.example.yaml" "$REPO/data/policy.yaml" \
    "data/policy.yaml —— 权限策略"
  # 白名单和管理员留空是 fail-closed 的默认状态，控制台填完即时生效
  copy_template "$REPO/config/qq.example.yaml" "$REPO/config/qq.yaml" \
    "config/qq.yaml —— 白名单为空，等控制台「信道」「权限」页填"
}

build_frontend() {
  say "构建控制台前端"
  local dir="$REPO/adapters/web/frontend"
  if [ -d "$dir/dist" ]; then
    skip "dist 已存在"
    return
  fi
  # 必须以 kazebot 身份跑，否则 dist 属主是 root，之后 deploy.sh 重建会 EACCES
  ( cd "$dir" && as_owner npm ci --silent && as_owner npm run build --silent )
  [ -d "$dir/dist" ] || fail "前端没构建出 dist —— 控制台会 404，而配置全靠它"
  info "dist 好了"
}

# ---------------------------------------------------------------- NapCat

setup_napcat() {
  say "拉起 NapCat"
  install -d -m 0755 "$NAPCAT_HOME/config" "$NAPCAT_HOME/qq" "$NAPCAT_HOME/logs"

  # 反向 WS 要在首次登录前配好：改完得重启容器，而重启会丢 QQ 登录态。
  # JSON 里不能有注释，NapCat 解析不了。
  if [ ! -f "$NAPCAT_HOME/config/onebot11.json" ]; then
    cat >"$NAPCAT_HOME/config/onebot11.json" <<EOF
{
  "network": {
    "websocketClients": [
      {
        "name": "kazebot",
        "enable": true,
        "url": "ws://127.0.0.1:$BOT_PORT/onebot/v11/ws",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "reconnectInterval": 5000,
        "token": "",
        "debug": false,
        "heartInterval": 30000
      }
    ],
    "httpServers": [],
    "httpSseServers": [],
    "httpClients": [],
    "websocketServers": [],
    "plugins": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": true
}
EOF
    info "反向 WS 指向 127.0.0.1:$BOT_PORT"
  fi

  if docker inspect napcat >/dev/null 2>&1; then
    skip "容器 napcat 已存在（重建会丢登录态，跳过）"
  else
    # 第一个号用 --network host。附件要以同一个绝对路径挂进容器，NapCat 是在自己的
    # 文件系统里 open() 那个路径的；挂载只能在 docker run 时定，忘了就得重建 + 重扫码。
    docker run -d \
      --name napcat \
      --restart always \
      --network host \
      -e WSR_ENABLE=true \
      -e NAPCAT_UID=0 -e NAPCAT_GID=0 -e TZ=Asia/Shanghai \
      -v "$NAPCAT_HOME/config:/app/napcat/config" \
      -v "$NAPCAT_HOME/qq:/app/.config/QQ" \
      -v "$NAPCAT_HOME/logs:/app/napcat/logs" \
      -v "$REPO/data/attachments:$REPO/data/attachments:ro" \
      "$IMAGE" >/dev/null
    info "容器起来了（$IMAGE）"
  fi

  info "等 WebUI 写出 token…"
  local waited=0
  while [ ! -s "$NAPCAT_HOME/config/webui.json" ]; do
    [ "$waited" -ge 90 ] && fail "容器 90 秒没写出 webui.json，看 docker logs napcat"
    sleep 2; waited=$((waited + 2))
  done

  # host 网络下 WebUI 默认监听 0.0.0.0，等于 6099 直接对公网开。必须夹回回环。
  local relisten
  relisten="$("$REPO/.venv/bin/python" - "$NAPCAT_HOME/config/webui.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
doc = json.loads(path.read_text(encoding="utf-8"))
if doc.get("host") == "127.0.0.1":
    print("no")
else:
    doc["host"] = "127.0.0.1"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print("yes")
PY
)"
  if [ "$relisten" = "yes" ]; then
    docker restart napcat >/dev/null
    info "WebUI 监听夹回 127.0.0.1，容器已重启"
    waited=0
    while [ ! -s "$NAPCAT_HOME/config/webui.json" ]; do
      [ "$waited" -ge 60 ] && fail "重启后 webui.json 没回来"
      sleep 2; waited=$((waited + 2))
    done
  fi

  # 原地改写 .env：换成 tmp+rename 会把属主变 root，supervisor 就读不到自己的配置了
  "$REPO/.venv/bin/python" - "$NAPCAT_HOME/config/webui.json" "$REPO/.env" <<'PY'
import json, pathlib, sys
token = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["token"]
env = pathlib.Path(sys.argv[2])
lines = env.read_text(encoding="utf-8").splitlines()
out = [f"NAPCAT_WEBUI_TOKEN={token}" if l.startswith("NAPCAT_WEBUI_TOKEN=") else l for l in lines]
if not any(l.startswith("NAPCAT_WEBUI_TOKEN=") for l in out):
    out.append(f"NAPCAT_WEBUI_TOKEN={token}")
env.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  info "WebUI token 回填进 .env（控制台扫码要用它）"
}

# ---------------------------------------------------------------- systemd

install_units() {
  say "装 systemd 单元"
  install -m 0644 -o root -g root \
    "$REPO/deploy/systemd/kazebot.service" \
    "$REPO/deploy/systemd/kazebot-qq.service" \
    /etc/systemd/system/
  systemctl daemon-reload
  info "kazebot.service / kazebot-qq.service"
}

start_and_verify() {
  say "启动并验证"
  local started_at; started_at=$(date +%s)
  systemctl enable --now kazebot >/dev/null 2>&1
  systemctl enable --now kazebot-qq >/dev/null 2>&1

  printf '    等 supervisor 应答'
  local waited=0
  until curl -fsS --max-time 2 "http://127.0.0.1:$SUP_PORT/v1/health" >/dev/null 2>&1; do
    [ "$waited" -ge 120 ] && { echo; fail "supervisor 120 秒没起来：journalctl -u kazebot -n 50"; }
    printf .; sleep 3; waited=$((waited + 3))
  done
  echo " —— ${waited}s"

  # systemd 只看得到 supervisor。engine 是它 spawn 的，崩了这里照样 active running。
  printf '    等 engine worker'
  waited=0
  while [ "$waited" -lt 90 ]; do
    pgrep -f "python -m engine" >/dev/null && break
    printf .; sleep 2; waited=$((waited + 2))
  done
  if ! pgrep -f "python -m engine" >/dev/null; then
    echo
    warn "engine worker 没起来 —— supervisor 活着也没人接任务"
    for log in "$REPO"/data/logs/engine-*.log; do
      [[ -f "$log" && $(stat -c %Y "$log") -ge $started_at ]] || continue
      echo "--- $log ---" >&2; tail -30 "$log" >&2
    done
    fail "先解决 engine 的问题再继续"
  fi
  echo " —— ${waited}s"

  ss -ltn | grep -q ":$BOT_PORT " \
    && info "NoneBot 在听 $BOT_PORT" \
    || warn "$BOT_PORT 没在听，NapCat 会连不上：journalctl -u kazebot-qq -n 50"

  curl -fsS --max-time 5 -H "Authorization: Bearer $(cat "$REPO/data/.admin_token")" \
    "http://127.0.0.1:$SUP_PORT/v1/qq/account" 2>/dev/null | grep -q '"configured": *true' \
    && info "NapCat 通道就绪，控制台可以扫码" \
    || warn "控制台还连不上 NapCat，扫码页可能是空的：docker logs napcat"
}

summary() {
  local token; token="$(cat "$REPO/data/.admin_token" 2>/dev/null || echo '<看 journalctl -u kazebot>')"
  cat <<EOF

$(printf '\033[1;32m装好了。剩下的全在控制台里点。\033[0m')

  控制台只听本机，从你自己的电脑先开条隧道：

      ssh -L $SUP_PORT:127.0.0.1:$SUP_PORT <你的用户>@<这台机器>

  然后浏览器打开：

      http://127.0.0.1:$SUP_PORT/web/?token=$token

  进去之后按这个顺序填，四步：

    1. 渠道    填模型 base_url、API key、模型名     ← 不填它不会说话
    2. 账号    手机扫码登录 bot 的 QQ 号
    3. 权限    填你自己的 QQ 号当管理员             ← 不填审批全自动拒绝
    4. 信道    填要让它待的群号                     ← 不填它不在任何群开口

  填完即时生效，不用重启。然后去群里 @ 它。

  之后：
    想多开（最多 9 个号）    sudo $REPO/deploy/install_provision.sh <第一个号的QQ号>
    更新代码                 sudo /usr/local/lib/kazebot/deploy.sh
    看日志                   journalctl -u kazebot -f

  提醒：
    · 容器重启会丢 QQ 登录态要重扫，调试时别顺手 docker restart napcat
    · 防火墙只放 22，$SUP_PORT/$BOT_PORT/$BRIDGE_PORT/$NAPCAT_PORT 一律不对外

EOF
}

# ---------------------------------------------------------------- main

main() {
  preflight
  install_packages
  create_user_and_dirs
  setup_venv
  write_env
  write_configs
  build_frontend
  setup_napcat
  install_units
  start_and_verify
  summary
}

main "$@"
