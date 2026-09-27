"""改 cloudflared 的 ingress。

同一条 tunnel 上还挂着别的站点，这个文件写坏就是全部一起掉线 —— 所以顺序、幂等和
校验失败时的不落盘都逐条钉住。
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "cloudflared_ingress.py"

_spec = importlib.util.spec_from_file_location("cloudflared_ingress", MODULE_PATH)
assert _spec and _spec.loader
ingress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingress)

LIVE = """\
tunnel: f09ea126
ingress:
  - hostname: my.example.com
    service: http://localhost:3000
  - hostname: qqbot.example.com
    service: http://localhost:8765
  - service: http_status:404
"""


@pytest.fixture()
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(LIVE, encoding="utf-8")
    return path


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """把 validate 与 reload 都接管掉，测试不碰真的 cloudflared。"""
    seen: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        seen.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _rules(config: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(config.read_text(encoding="utf-8"))["ingress"]


def test_new_rule_lands_before_the_catch_all_for_the_same_host(config: Path, calls: list[Any]) -> None:
    # ingress 顺序匹配。排在无 path 那条之后，新实例永远收不到流量。
    ingress.add(config, "1000000002", 8775, 8765)
    rules = _rules(config)
    paths = [r.get("path") for r in rules]
    assert paths.index("^/i/1000000002(/|$)") < paths.index(None, 2)
    assert rules[-1] == {"service": "http_status:404"}


def test_rule_reuses_the_primary_hostname(config: Path, calls: list[Any]) -> None:
    ingress.add(config, "1000000002", 8775, 8765)
    added = next(r for r in _rules(config) if r.get("path"))
    assert added["hostname"] == "qqbot.example.com"
    assert added["service"] == "http://localhost:8775"


def test_other_sites_are_untouched(config: Path, calls: list[Any]) -> None:
    before = [r for r in _rules(config) if r.get("hostname") == "my.example.com"]
    ingress.add(config, "1000000002", 8775, 8765)
    assert [r for r in _rules(config) if r.get("hostname") == "my.example.com"] == before


def test_path_pattern_also_matches_without_the_trailing_slash(config: Path, calls: list[Any]) -> None:
    # 只写 ^/i/<实例名>/ 的话，访问 /i/<实例名> 会漏给主实例，看起来像是切换失败。
    ingress.add(config, "koki", 8775, 8765)
    assert next(r for r in _rules(config) if r.get("path"))["path"] == "^/i/koki(/|$)"


@pytest.mark.parametrize("instance", ["koki", "bot_2", "1000000002"])
def test_the_rule_path_is_anchored_on_the_instance_name(instance: str) -> None:
    assert ingress._rule_path(instance) == f"^/i/{instance}(/|$)"


def test_adding_twice_is_a_no_op(config: Path, calls: list[Any]) -> None:
    ingress.add(config, "1000000002", 8775, 8765)
    count = len(_rules(config))
    ingress.add(config, "1000000002", 8775, 8765)
    assert len(_rules(config)) == count


def test_remove_takes_out_only_that_rule(config: Path, calls: list[Any]) -> None:
    ingress.add(config, "1000000002", 8775, 8765)
    ingress.add(config, "1828838999", 8785, 8765)
    ingress.remove(config, "1000000002")
    paths = [r.get("path") for r in _rules(config)]
    assert "^/i/1000000002(/|$)" not in paths
    assert "^/i/1828838999(/|$)" in paths


def test_remove_of_an_absent_rule_leaves_the_file_alone(config: Path, calls: list[Any]) -> None:
    before = config.read_text(encoding="utf-8")
    ingress.remove(config, "1000000002")
    assert config.read_text(encoding="utf-8") == before
    assert calls == []


def test_missing_primary_rule_aborts_instead_of_guessing(tmp_path: Path, calls: list[Any]) -> None:
    path = tmp_path / "config.yml"
    path.write_text("ingress:\n  - service: http_status:404\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="找不到指向 8765"):
        ingress.add(path, "1000000002", 8775, 8765)


def test_failed_validation_leaves_the_live_config_untouched(
    config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = config.read_text(encoding="utf-8")

    def refuse(command: list[str], **_kwargs: Any) -> Any:
        if "validate" in command:
            return SimpleNamespace(returncode=1, stdout="bad ingress", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", refuse)
    with pytest.raises(SystemExit, match="校验没过"):
        ingress.add(config, "1000000002", 8775, 8765)
    assert config.read_text(encoding="utf-8") == before


def test_no_temp_file_is_left_behind_after_a_rejected_write(
    config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    with pytest.raises(SystemExit):
        ingress.add(config, "1000000002", 8775, 8765)
    assert list(config.parent.glob("*.yml")) == [config]


def test_a_backup_is_kept_next_to_the_config(config: Path, calls: list[Any]) -> None:
    ingress.add(config, "1000000002", 8775, 8765)
    assert list(config.parent.glob("config.yml.bak-*"))


def test_reload_failure_rolls_back(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 写进去了但 cloudflared 起不来，比不改还糟：整条 tunnel 上的站点全没了。
    before = config.read_text(encoding="utf-8")

    def flaky(command: list[str], **_kwargs: Any) -> Any:
        ok = "validate" in command
        return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", flaky)
    with pytest.raises(SystemExit, match="重载失败"):
        ingress.add(config, "1000000002", 8775, 8765)
    assert config.read_text(encoding="utf-8") == before


def test_reload_prefers_reload_over_restart(config: Path, calls: list[list[str]]) -> None:
    # restart 会断开所有站点几秒，reload 能成就不要 restart。
    ingress.add(config, "1000000002", 8775, 8765)
    systemctl = [c for c in calls if c and c[0] == "systemctl"]
    assert systemctl and systemctl[0][1] == "reload"
    assert not any(c[1] == "restart" for c in systemctl)


# ── 实例名 ──
# 这个脚本跑在 root 侧，实例名会拼进落盘的 ingress 路径正则；宽一格就是一条提权面。

@pytest.mark.parametrize("instance", ["koki", "bot_2", "ab", "x" * 32, "1000000002", "12345678901"])
def test_a_plausible_instance_name_passes(instance: str) -> None:
    assert ingress.INSTANCE_ID.match(instance)


@pytest.mark.parametrize("bad", [
    "", "a", "1234", "0123456", "1abc", "Koki", "bot-2", "x" * 33, "1234567890123",
    "koki/../rong", "koki|rong", "koki(/|$)", "koki\nrong", "12345 67890", "12345$(id)",
])
def test_a_bad_instance_name_never_matches(bad: str) -> None:
    assert not ingress.INSTANCE_ID.match(bad)


@pytest.mark.parametrize("instance", ["Koki", "bot-2", "stickers", "../rong", ""])
def test_the_entry_point_refuses_a_name_it_should_not_touch(
    config: Path, calls: list[Any], monkeypatch: pytest.MonkeyPatch, instance: str,
) -> None:
    # stickers 是数据根下的共享表情包库，不是实例。
    monkeypatch.setattr(
        "sys.argv",
        ["cloudflared_ingress.py", "add", "--config", str(config), "--instance", instance, "--port", "8775"],
    )
    with pytest.raises(SystemExit, match="实例名不合法"):
        ingress.main()
    assert calls == []


@pytest.mark.parametrize("port", ["0", "80", "1024", "65536", "99999"])
def test_the_entry_point_refuses_a_port_outside_the_unprivileged_range(
    config: Path, calls: list[Any], monkeypatch: pytest.MonkeyPatch, port: str,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["cloudflared_ingress.py", "add", "--config", str(config), "--instance", "koki", "--port", port],
    )
    with pytest.raises(SystemExit, match="port 不合法"):
        ingress.main()
    assert calls == []


def test_the_entry_point_routes_a_good_name_through(
    config: Path, calls: list[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["cloudflared_ingress.py", "add", "--config", str(config), "--instance", "koki", "--port", "8775"],
    )
    ingress.main()
    assert next(r for r in _rules(config) if r.get("path"))["path"] == "^/i/koki(/|$)"
