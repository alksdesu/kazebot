#!/usr/bin/env bash
# 自建 NapCat Shell + Linux QQ 镜像。腾讯撤了旧版 deb、上游镜像仓库也没了，QQ 安装包和 NapCat.Shell.zip 自备。
#
# 用法: build.sh --qq <linuxqq_*.deb、已解包目录或其 tar 包> --shell <NapCat.Shell.zip>
#              [--machine-id <32 位十六进制>] [--tag <repo:tag>] [--context-only <目录>]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=image.env
. "$HERE/image.env"

QQ="" SHELL_ZIP="" MACHINE_ID="" TAG="$NAPCAT_IMAGE" CONTEXT_ONLY=""
usage() { sed -n '2,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --qq) QQ="${2:-}"; shift 2 ;;
    --shell) SHELL_ZIP="${2:-}"; shift 2 ;;
    --machine-id) MACHINE_ID="${2:-}"; shift 2 ;;
    --tag) TAG="${2:-}"; shift 2 ;;
    --context-only) CONTEXT_ONLY="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$QQ" ] && [ -n "$SHELL_ZIP" ] || usage
[ -e "$QQ" ] || { echo "找不到 QQ 安装包或目录：$QQ" >&2; exit 1; }
[ -f "$SHELL_ZIP" ] || { echo "找不到 NapCat.Shell.zip：$SHELL_ZIP" >&2; exit 1; }
if [ -n "$MACHINE_ID" ] && ! [[ "$MACHINE_ID" =~ ^[0-9a-f]{32}$ ]]; then
  echo "machine-id 要是 32 位小写十六进制：$MACHINE_ID" >&2; exit 1
fi

if [ -n "$CONTEXT_ONLY" ]; then
  CONTEXT="$CONTEXT_ONLY"; mkdir -p "$CONTEXT"
else
  CONTEXT="$(mktemp -d)"; trap 'rm -rf "$CONTEXT"' EXIT
fi

# deb 只拆不装：postinst 的事 Dockerfile 里做。没有 dpkg 的宿主走 ar + tar。
unpack_deb() {
  local deb="$1" dest="$2"
  if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb -x "$deb" "$dest"
  else
    local work; work="$(mktemp -d)"
    (cd "$work" && ar x "$(realpath "$deb")" && tar -xf data.tar.* -C "$dest")
    rm -rf "$work"
  fi
}

echo "==> 准备 QQ"
mkdir -p "$CONTEXT/qq"
case "$QQ" in
  *.deb) unpack_deb "$QQ" "$CONTEXT/qq" ;;
  *.tar|*.tar.gz|*.tgz|*.tar.xz|*.tar.zst)
    # 顶层多一层目录（qq34/opt）或直接是 opt/ 都认
    tar -xf "$QQ" -C "$CONTEXT/qq"
    if [ ! -d "$CONTEXT/qq/opt" ]; then
      inner="$(find "$CONTEXT/qq" -mindepth 1 -maxdepth 1 -type d | head -1)"
      [ -n "$inner" ] && mv "$inner"/opt "$inner"/usr "$CONTEXT/qq/" && rmdir "$inner"
    fi ;;
  *) [ -d "$QQ" ] || { echo "--qq 要是 deb、tar 包或已解包目录：$QQ" >&2; exit 1; }
     cp -a "$QQ"/opt "$QQ"/usr "$CONTEXT/qq/" ;;
esac
APP="$CONTEXT/qq/opt/QQ/resources/app"
[ -f "$APP/wrapper.node" ] && [ -f "$APP/package.json" ] && [ -x "$CONTEXT/qq/opt/QQ/qq" ] \
  || { echo "QQ 目录不完整：缺 opt/QQ/qq、resources/app/wrapper.node 或 package.json" >&2; exit 1; }
# macOS 打的包会带 AppleDouble 文件
find "$CONTEXT/qq" -name '._*' -delete
QQ_VERSION="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$APP/package.json" | head -1)"

echo "==> 准备 NapCat"
cp "$SHELL_ZIP" "$CONTEXT/NapCat.Shell.zip"
cp "$HERE/Dockerfile" "$HERE/entrypoint.sh" "$CONTEXT/"
NAPCAT_VERSION="$(unzip -p "$CONTEXT/NapCat.Shell.zip" napcat.mjs 2>/dev/null | grep -oE 'NapCat\.Core Version: [^"]*"\+"[0-9.]+|"4\.[0-9]+\.[0-9]+"' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
unzip -l "$CONTEXT/NapCat.Shell.zip" napcat.mjs >/dev/null 2>&1 || { echo "zip 里没有 napcat.mjs，不是 NapCat.Shell.zip" >&2; exit 1; }

# 不指定就沿用现役容器的，重建容器时 QQ 看到的机器不变。
if [ -z "$MACHINE_ID" ] && command -v docker >/dev/null 2>&1; then
  running="$(docker ps --filter 'name=^napcat' --format '{{.Names}}' 2>/dev/null | head -1 || true)"
  if [ -n "$running" ]; then
    MACHINE_ID="$(docker exec "$running" cat /etc/machine-id 2>/dev/null | tr -d '\n' || true)"
    [[ "$MACHINE_ID" =~ ^[0-9a-f]{32}$ ]] || MACHINE_ID=""
  fi
fi

echo "    QQ $QQ_VERSION / NapCat ${NAPCAT_VERSION:-?} / machine-id ${MACHINE_ID:-基础镜像自带} / 标签 $TAG"
if [ -n "$CONTEXT_ONLY" ]; then
  printf 'MACHINE_ID=%s\nTAG=%s\nQQ_VERSION=%s\nNAPCAT_VERSION=%s\n' "$MACHINE_ID" "$TAG" "$QQ_VERSION" "$NAPCAT_VERSION" > "$CONTEXT/build.args"
  echo "==> 构建上下文已放在 $CONTEXT"
  exit 0
fi

echo "==> docker build $TAG"
docker build --build-arg "MACHINE_ID=$MACHINE_ID" -t "$TAG" "$CONTEXT"
echo "==> 好了：$TAG"
