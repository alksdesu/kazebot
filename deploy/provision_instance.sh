#!/usr/bin/env bash
# root 侧建号/删号：docker、systemd、cloudflared。
#
# 必须装到 /usr/local/lib/kazebot/（见 install_provision.sh）。留在 /opt/kazebot 下
# 就是提权洞：那里 kazebot 可写，而 agent 能以 kazebot 身份执行任意命令。
# 同理这里不 source 工作区 .env、不用 /opt/kazebot/.venv 的解释器。
set -euo pipefail

DATA_ROOT=/opt/kazebot-data
REPO=/opt/kazebot
UNITS=/usr/local/lib/kazebot/units
PY=/usr/bin/python3
CF_EDIT=/usr/local/lib/kazebot/cloudflared_ingress.py
CF_CONF=/etc/cloudflared/config.yml
OWNER=kazebot

ACTION="${1:-}"
UIN="${2:-}"

[[ "$UIN" =~ ^[1-9][0-9]{4,10}$ ]] || { echo "uin 不合法" >&2; exit 2; }
[[ "$ACTION" == "create" || "$ACTION" == "remove" ]] || { echo "action 不合法" >&2; exit 2; }

WS="$DATA_ROOT/$UIN"
NC_HOME="/opt/napcat-$UIN"
# 放在工作区外：删号会把工作区改名，日志跟着搬走的话控制台就永远等不到结果。
LOG="$DATA_ROOT/provision-$UIN.log"

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
fail() { printf 'DONE fail: %s\n' "$*" >>"$LOG"; echo "$*" >&2; exit 1; }
trap 'fail "第 $LINENO 行执行失败"' ERR

# 只取需要的键，并且逐个按形状校验 —— .env 是 kazebot 可写的，当脚本输入而非代码看待。
env_get() {
  local key="$1" want="$2" value
  value="$(sed -n "s/^${key}=//p" "$WS/.env" | head -1 | tr -d '\r' | tr -d '"')"
  [[ "$value" =~ $want ]] || fail "$WS/.env 里 $key 的值不合形状"
  printf '%s' "$value"
}

# ---------- create ----------

do_create() {
  [ -d "$WS" ] || fail "工作区 $WS 不存在，应该由控制台先建好"
  [ -f "$WS/.env" ] || fail "$WS/.env 不存在"

  local sup_port bot_port napcat_port access_token image
  sup_port="$(env_get CLONOTH_PORT '^[0-9]{4,5}$')"
  bot_port="$(env_get PORT '^[0-9]{4,5}$')"
  napcat_port="$(sed -n 's|^NAPCAT_WEBUI_URL=http://127\.0\.0\.1:||p' "$WS/.env" | head -1 | tr -d '\r')"
  [[ "$napcat_port" =~ ^[0-9]{4,5}$ ]] || fail "NAPCAT_WEBUI_URL 不合形状"
  access_token="$(env_get ONEBOT_ACCESS_TOKEN '^[0-9a-f]{16,64}$')"

  say "实例 $UIN：supervisor $sup_port / bot $bot_port / napcat $napcat_port"

  # 版本跟现役容器走，避免新号悄悄用上另一个 NapCat。
  image="$(docker inspect napcat --format '{{.Config.Image}}' 2>/dev/null || true)"
  [ -n "$image" ] || fail "读不到现有 napcat 容器的镜像，先确认它在跑"

  if docker inspect "napcat-$UIN" >/dev/null 2>&1; then
    say "容器 napcat-$UIN 已存在，跳过创建"
  else
    say "拉起容器 napcat-$UIN（$image）"
    mkdir -p "$NC_HOME/config" "$NC_HOME/qq"
    # 反向 WS 的模板：新号登录后 NapCat 从 onebot11.json 派生自己那份。
    "$PY" - "$NC_HOME/config/onebot11.json" "$bot_port" "$access_token" <<'PY'
import json, sys
path, port, token = sys.argv[1], int(sys.argv[2]), sys.argv[3]
json.dump({
    "network": {
        "websocketClients": [{
            "name": "kazebot", "enable": True,
            "url": f"ws://host.docker.internal:{port}/onebot/v11/ws",
            "messagePostFormat": "array", "reportSelfMessage": False,
            "reconnectInterval": 5000, "token": token,
            "debug": False, "heartInterval": 30000,
        }],
        "httpServers": [], "httpSseServers": [], "httpClients": [],
        "websocketServers": [], "plugins": [],
    },
    "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": True,
}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
    # 序号 0 那个跑 --network host，第二个 host 容器会抢 6099 和 QQ NT 占的 4001/4301。
    docker run -d --name "napcat-$UIN" --restart always \
      -p "127.0.0.1:$napcat_port:6099" \
      --add-host "host.docker.internal:host-gateway" \
      -e NAPCAT_UID=0 -e NAPCAT_GID=0 -e TZ=Asia/Shanghai \
      -v "$NC_HOME/config:/app/napcat/config" \
      -v "$NC_HOME/qq:/app/.config/QQ" \
      -v "$WS/data/attachments:$WS/data/attachments" \
      "$image" >/dev/null
  fi

  say "等 WebUI 生成 token…"
  local waited=0
  while [ ! -s "$NC_HOME/config/webui.json" ]; do
    [ "$waited" -ge 90 ] && fail "容器 90 秒没写出 webui.json，docker logs napcat-$UIN 看看"
    sleep 2; waited=$((waited + 2))
  done

  # 原地改写：换成临时文件再 rename 会把属主变成 root，supervisor 就读不到自己的 .env 了。
  "$PY" - "$NC_HOME/config/webui.json" "$WS/.env" <<'PY'
import json, pathlib, sys
token = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["token"]
env = pathlib.Path(sys.argv[2])
lines = env.read_text(encoding="utf-8").splitlines()
out = [f"NAPCAT_WEBUI_TOKEN={token}" if l.startswith("NAPCAT_WEBUI_TOKEN=") else l for l in lines]
if not any(l.startswith("NAPCAT_WEBUI_TOKEN=") for l in out):
    out.append(f"NAPCAT_WEBUI_TOKEN={token}")
with env.open("w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
PY
  say "WebUI token 已回填 .env"

  say "装 systemd 单元"
  [ -d "$UNITS" ] || fail "$UNITS 不存在，先跑 install_provision.sh"
  install -m 0644 -o root -g root "$UNITS"/kazebot@.service "$UNITS"/kazebot-qq@.service \
    "$UNITS"/kazebot-cleanup@.service "$UNITS"/kazebot-cleanup@.timer /etc/systemd/system/
  systemctl daemon-reload

  say "接入 cloudflared"
  "$PY" "$CF_EDIT" add --config "$CF_CONF" --uin "$UIN" --port "$sup_port" | tee -a "$LOG"

  say "启动服务"
  chown -R "$OWNER:$OWNER" "$WS"
  systemctl enable --now "kazebot@$UIN" "kazebot-qq@$UIN" "kazebot-cleanup@$UIN.timer"

  say "等 supervisor 应答 127.0.0.1:$sup_port …"
  waited=0
  until curl -fsS --max-time 2 "http://127.0.0.1:$sup_port/v1/health" >/dev/null 2>&1; do
    [ "$waited" -ge 120 ] && fail "服务 120 秒没起来，journalctl -u kazebot@$UIN 看看"
    sleep 3; waited=$((waited + 3))
  done

  say "完成。去 /i/$UIN/web/ 扫码登录这个号。"
  printf 'DONE ok\n' >>"$LOG"
}

# ---------- remove ----------

do_remove() {
  # 纵深防御：supervisor 侧已挡过一次，这里再确认删的不是主实例。
  [ "$WS" != "$REPO" ] || fail "拒绝操作代码目录"
  [ -d "$WS" ] || fail "工作区 $WS 不存在"
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"

  say "停服务"
  systemctl disable --now "kazebot@$UIN" "kazebot-qq@$UIN" "kazebot-cleanup@$UIN.timer" 2>&1 | tee -a "$LOG" || true

  # 名字精确到 napcat-<数字>，永远撞不上序号 0 的 napcat，也撞不上 gitea。
  if docker inspect "napcat-$UIN" >/dev/null 2>&1; then
    say "移除容器 napcat-$UIN"
    docker rm -f "napcat-$UIN" >/dev/null
  fi

  say "摘掉 cloudflared 规则"
  "$PY" "$CF_EDIT" remove --config "$CF_CONF" --uin "$UIN" | tee -a "$LOG"

  # 由完成方摘清单，不由发起方乐观预摘：root 这边失败的话，控制台里那条还在，
  # 至少不会出现「清单查无此号、进程却还在跑」的幽灵。
  # 锁的是 supervisor 侧用的同一个文件，否则两边同时改会丢条目。
  say "从清单摘掉 $UIN"
  flock "$DATA_ROOT/instances.yaml.lock" "$PY" - "$DATA_ROOT/instances.yaml" "$UIN" <<'PY'
import pathlib, sys, yaml
path, uin = pathlib.Path(sys.argv[1]), sys.argv[2]
try:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
except OSError:
    sys.exit(0)
rows = doc.get("instances") if isinstance(doc, dict) else None
if not isinstance(rows, list):
    sys.exit(0)
kept = [r for r in rows if not (isinstance(r, dict) and str(r.get("uin")) == uin)]
tmp = path.with_name(path.name + ".tmp")
tmp.write_text(yaml.safe_dump({"instances": kept}, allow_unicode=True, sort_keys=False), encoding="utf-8")
tmp.replace(path)
PY
  # flock 可能刚以 root 建出锁文件，不还回去 supervisor 下次就锁不上了。
  chown "$OWNER:$OWNER" "$DATA_ROOT/instances.yaml" "$DATA_ROOT/instances.yaml.lock" 2>/dev/null || true

  # 归档而不是删除：这个脚本里不存在递归删除，被骗着跑一次也丢不了数据。
  say "工作区归档为 $WS.deleted-$stamp"
  mv "$WS" "$WS.deleted-$stamp"
  if [ -d "$NC_HOME" ]; then
    mv "$NC_HOME" "$NC_HOME.deleted-$stamp"
  fi

  say "完成。数据留在 .deleted-$stamp，确认无误后自行清理。"
  printf 'DONE ok\n' >>"$LOG"
}

touch "$LOG"
case "$ACTION" in
  create) do_create ;;
  remove) do_remove ;;
esac
