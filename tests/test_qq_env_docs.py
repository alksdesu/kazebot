"""QQ 文档里写的环境变量必须真的能生效。

只存在于文档的旋钮比没有文档更坏：照着改完 env、重启、什么都没变，
最后被怀疑的是 bot 而不是文档。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from _onebot_harness import load_live_config  # noqa: E402

_DOCS = (
    "docs/TRIGGER-GUIDE.md",
    "docs/QQBOT-MECHANISM.md",
    "docs/MEMORY-STATUS.md",
    "README.md",
    "deploy/INSTALL.md",
    "adapters/onebot/README.md",
    "config/qq.example.yaml",
)
_SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", "tests", "_research"}
_ENV_RE = re.compile(r"\b(?:ONEBOT|CLONOTH)_[A-Z0-9_]+")
# nonebot-adapter-onebot 自己读这两个，本仓库代码里不会出现。
_EXTERNAL_ENV = frozenset({"ONEBOT_ACCESS_TOKEN", "ONEBOT_SECRET"})
_REMOVED_ENV = ("ONEBOT_IMAGE_TIMEOUT_RESEND_DELAY_SEC", "ONEBOT_IMAGE_PREFER_SAME_SENDER")


@pytest.fixture(scope="module")
def source_blob() -> str:
    chunks: list[str] = []
    for path in _ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.relative_to(_ROOT).parts):
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(chunks)


@pytest.fixture(scope="module")
def doc_texts() -> dict[str, str]:
    # docs/ 不进公开仓库。缺哪份就少校验哪份，不能因此把仓库里那几份的把关也一起放掉。
    return {
        rel: (_ROOT / rel).read_text(encoding="utf-8")
        for rel in _DOCS
        if (_ROOT / rel).exists()
    }


def _table_rows(text: str) -> list[tuple[int, str]]:
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if line.lstrip().startswith("|")
    ]


def test_documented_env_names_are_read_by_code(doc_texts: dict[str, str], source_blob: str) -> None:
    # 只扫表格行：TRIGGER-GUIDE §4/§5 的 python 代码块写的是提案键，不是现有清单。
    for rel, text in doc_texts.items():
        for number, line in _table_rows(text):
            for name in _ENV_RE.findall(line):
                if name.endswith("_") or name in _EXTERNAL_ENV:
                    continue
                # 判据松到「文本里出现过」：槽位 env 名在 clonoth_runtime.py 里是前缀拼出来的。
                assert name in source_blob, f"{rel}:{number} 写了 {name}，但代码里没有这个名字"


@pytest.mark.parametrize("name", _REMOVED_ENV)
def test_removed_knobs_stay_removed(name: str, doc_texts: dict[str, str], source_blob: str) -> None:
    assert name not in source_blob, f"{name} 又回到代码里了"
    for rel, text in doc_texts.items():
        assert name not in text, f"{rel} 又开始宣传已删除的 {name}"


def test_no_dead_config_claims_in_docs(doc_texts: dict[str, str], source_blob: str) -> None:
    for rel, text in doc_texts.items():
        for number, line in enumerate(text.splitlines(), start=1):
            if "死配置" not in line:
                continue
            for name in _ENV_RE.findall(line):
                assert name not in source_blob, (
                    f"{rel}:{number} 把 {name} 说成死配置，但代码里还在读它"
                )


def test_workspace_default_is_derived_from_the_repo(
    monkeypatch: pytest.MonkeyPatch, doc_texts: dict[str, str],
) -> None:
    monkeypatch.delenv("CLONOTH_WORKSPACE", raising=False)
    monkeypatch.delenv("ONEBOT_WORKSPACE_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location(
        "_qq_env_docs_config", _ROOT / "adapters" / "onebot" / "config.py",
    )
    assert spec and spec.loader
    config = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_qq_env_docs_config", config)
    spec.loader.exec_module(config)
    assert Path(config.CLONOTH_WORKSPACE) == _ROOT
    for rel, text in doc_texts.items():
        for number, line in _table_rows(text):
            assert "/www/wwwroot/Clonoth" not in line, (
                f"{rel}:{number} 还把 /www/wwwroot/Clonoth 当默认工作区"
            )


@pytest.fixture(scope="module")
def console_blob() -> str:
    root = _ROOT / "adapters/web/frontend/src/console"
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.ts*"))
    )


# 被七个信号开关取代的旧枚举。控制台只读它推导「这项没配时当前行为是什么」的提示，
# 给它控件会让同一个判定有两套入口，改哪边都可能被另一边悄悄盖掉。
_CONSOLE_READ_ONLY_KEYS = frozenset({"group_trigger"})


def test_every_live_key_has_a_console_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, console_blob: str,
) -> None:
    live_config = load_live_config(monkeypatch, tmp_path)
    # 只认 configKey=：useLiveValue('x') 那种只读消费不是控件，按文本搜会漏报成已覆盖。
    wired = set(re.findall(r'configKey="([a-z_]+)"', console_blob))
    missing = [
        key.name
        for key in live_config.LIVE_KEYS
        # 能力档位的控件是 `grant_${cap.key}` 拼出来的，没有字面量；下一条断言盯它。
        if not key.name.startswith("grant_")
        and key.name not in _CONSOLE_READ_ONLY_KEYS
        and key.name not in wired
    ]
    assert not missing, f"这些热载键在控制台上没有控件，只能手改 yaml：{missing}"


def test_read_only_console_keys_really_have_no_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, console_blob: str,
) -> None:
    live_config = load_live_config(monkeypatch, tmp_path)
    known = {key.name for key in live_config.LIVE_KEYS}
    assert _CONSOLE_READ_ONLY_KEYS <= known, "豁免名单里有已经不存在的键"
    wired = set(re.findall(r'configKey="([a-z_]+)"', console_blob))
    leaked = sorted(_CONSOLE_READ_ONLY_KEYS & wired)
    assert not leaked, f"这些键被当成 deprecated 豁免了，却又给了控件：{leaked}"


def test_capability_grants_line_up_with_the_console_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    live_config = load_live_config(monkeypatch, tmp_path)
    granted = {key.name for key in live_config.LIVE_KEYS if key.name.startswith("grant_")}
    published = {f"grant_{item['key']}" for item in live_config.capability.catalog()}
    assert granted == published, (
        f"能力档位与 /qq/state 公布的清单不一致，界面会无声缺席：{granted ^ published}"
    )
