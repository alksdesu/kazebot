#!/usr/bin/env python3
"""给 cloudflared 的 ingress 加/摘一条实例路由。

跑在 root 侧，只用标准库 + 系统 pyyaml —— 不碰 /opt/kazebot 的 venv，那里 kazebot 可写。
同一条 tunnel 上还挂着别的站点，所以永远先备份、validate 通过才落盘。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

CLOUDFLARED = "/usr/local/bin/cloudflared"
# 和 supervisor 侧、建实例脚本、polkit 规则同一条：实例名会拼进 ingress 的路径正则。
INSTANCE_ID = re.compile(r"^(?:[a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10})$")


class _Dumper(yaml.SafeDumper):
    """保住列表的缩进。safe_dump 默认吐平的 `- x`，跟手写的配置 diff 起来满屏都是噪声。"""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def _rule_path(instance: str) -> str:
    # 收尾用 (/|$)：只写 ^/i/<实例名>/ 的话，不带尾斜杠的访问会漏给主实例。
    return f"^/i/{instance}(/|$)"


def _load(config: Path) -> dict[str, Any]:
    doc = yaml.safe_load(config.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("ingress"), list):
        raise SystemExit(f"{config} 里没有 ingress 列表")
    return doc


def _base_hostname(rules: list[Any], base_port: int) -> str:
    """主实例那条规则的域名。几个实例共用它，只按路径分流。"""
    wanted = {f"http://localhost:{base_port}", f"http://127.0.0.1:{base_port}"}
    for rule in rules:
        if isinstance(rule, dict) and rule.get("service") in wanted and rule.get("hostname"):
            return str(rule["hostname"])
    raise SystemExit(f"找不到指向 {base_port} 的主实例规则，无法推断域名")


def _write_checked(config: Path, doc: dict[str, Any]) -> None:
    body = yaml.dump(doc, Dumper=_Dumper, allow_unicode=True, sort_keys=False, default_flow_style=False)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".yml", dir=str(config.parent), delete=False,
    ) as handle:
        handle.write(body)
        candidate = Path(handle.name)
    try:
        done = subprocess.run(
            [CLOUDFLARED, "--config", str(candidate), "tunnel", "ingress", "validate"],
            capture_output=True, text=True, timeout=60,
        )
        if done.returncode != 0:
            raise SystemExit(f"ingress 校验没过，原配置未动：\n{done.stdout}{done.stderr}")
        backup = config.with_name(f"{config.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(config, backup)
        candidate.chmod(0o644)
        os.replace(candidate, config)
        print(f"    已写入，原配置备份在 {backup}")
    finally:
        candidate.unlink(missing_ok=True)

    _reload(config, backup)


def _reload(config: Path, backup: Path) -> None:
    """重载失败就把备份放回去 —— 同一条 tunnel 上还有别的站点，不能让它们陪葬。"""
    for command in (["systemctl", "reload", "cloudflared"], ["systemctl", "restart", "cloudflared"]):
        done = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if done.returncode == 0:
            print(f"    {' '.join(command[1:])} 成功")
            return
    shutil.copy2(backup, config)
    subprocess.run(["systemctl", "restart", "cloudflared"], capture_output=True, timeout=90)
    raise SystemExit("cloudflared 重载失败，已回滚到备份并重启")


def add(config: Path, instance: str, port: int, base_port: int) -> None:
    doc = _load(config)
    rules: list[Any] = doc["ingress"]
    path = _rule_path(instance)
    if any(isinstance(r, dict) and r.get("path") == path for r in rules):
        print(f"    /i/{instance} 的规则已存在，跳过")
        return

    hostname = _base_hostname(rules, base_port)
    entry = {"hostname": hostname, "path": path, "service": f"http://localhost:{port}"}
    # 必须插在同域名那条无 path 规则之前：ingress 是顺序匹配，排在后面永远轮不到。
    for index, rule in enumerate(rules):
        if isinstance(rule, dict) and rule.get("hostname") == hostname and not rule.get("path"):
            rules.insert(index, entry)
            break
    else:
        rules.insert(max(len(rules) - 1, 0), entry)

    _write_checked(config, doc)
    print(f"    {hostname}/i/{instance}/ → localhost:{port}")


def remove(config: Path, instance: str) -> None:
    doc = _load(config)
    path = _rule_path(instance)
    kept = [r for r in doc["ingress"] if not (isinstance(r, dict) and r.get("path") == path)]
    if len(kept) == len(doc["ingress"]):
        print(f"    /i/{instance} 没有对应规则，跳过")
        return
    doc["ingress"] = kept
    _write_checked(config, doc)
    print(f"    已摘掉 /i/{instance}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("add", "remove"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--base-port", type=int, default=8765)
    args = parser.parse_args()

    if not INSTANCE_ID.match(args.instance) or args.instance == "stickers":
        raise SystemExit("实例名不合法")
    if args.action == "add":
        if not 1024 < args.port < 65536:
            raise SystemExit("port 不合法")
        add(args.config, args.instance, args.port, args.base_port)
    else:
        remove(args.config, args.instance)


if __name__ == "__main__":
    sys.exit(main())
