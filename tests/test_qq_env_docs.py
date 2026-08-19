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
    "docs/DEPLOY-QQ.md",
    "docs/TRIGGER-GUIDE.md",
    "docs/QQBOT-MECHANISM.md",
    "docs/MEMORY-STATUS.md",
    "README.md",
    "adapters/onebot/README.md",
    "config/qq.example.yaml",
)
_SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", "tests", "_research"}
_ENV_RE = re.compile(r"\b(?:ONEBOT|CLONOTH)_[A-Z0-9_]+")
# nonebot-adapter-onebot 自己读这两个，本仓库代码里不会出现。
_EXTERNAL_ENV = frozenset({"ONEBOT_ACCESS_TOKEN", "ONEBOT_SECRET"})
_REMOVED_ENV = ("ONEBOT_IMAGE_TIMEOUT_RESEND_DELAY_SEC", "ONEBOT_IMAGE_PREFER_SAME_SENDER")
_DEPLOY_DOC = "docs/DEPLOY-QQ.md"

# docs/ 和 README.md 不进公开仓库，clone 出来的检出没有可校验的对象。
pytestmark = pytest.mark.skipif(
    not (_ROOT / _DEPLOY_DOC).exists(),
    reason="此检出不含 docs/，文档一致性校验跳过",
)


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
    return {rel: (_ROOT / rel).read_text(encoding="utf-8") for rel in _DOCS}


def _table_rows(text: str) -> list[tuple[int, str]]:
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if line.lstrip().startswith("|")
    ]


def _env_inventory_rows(text: str) -> list[tuple[int, str]]:
    """DEPLOY-QQ §6 的表格行。同一个变量在别处的症状表/排障表里还会出现，不算清单。"""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## 6. "))
    end = next(i for i, line in enumerate(lines[start + 1:], start=start + 1) if line.startswith("## "))
    return [
        (number, line)
        for number, line in enumerate(lines[start:end], start=start + 1)
        if line.lstrip().startswith("|")
    ]


def _first_cell(row: str) -> str:
    return row.strip().strip("|").split("|")[0]


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


def test_env_backed_live_keys_are_marked_hot_reloadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, doc_texts: dict[str, str],
) -> None:
    live_config = load_live_config(monkeypatch, tmp_path)
    rows = _env_inventory_rows(doc_texts[_DEPLOY_DOC])
    for key in live_config.LIVE_KEYS:
        if not key.env:
            continue
        primary = key.env[0]
        hits = [
            (number, line) for number, line in rows
            if re.search(rf"\b{primary}\b", _first_cell(line))
        ]
        assert len(hits) == 1, f"{_DEPLOY_DOC} §6 里 {primary} 占了 {len(hits)} 行，应当恰好 1 行"
        number, line = hits[0]
        assert "🔄" in line, f"{_DEPLOY_DOC}:{number} 的 {primary} 是热载键，缺 🔄 标记"
        assert key.path in line, f"{_DEPLOY_DOC}:{number} 的 {primary} 没写热载键路径 {key.path}"


def test_nothing_is_marked_hot_reloadable_without_being_a_live_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, doc_texts: dict[str, str],
) -> None:
    """标了 🔄 却不是热载键，会让人改完 env 干等着不重启。"""
    live_config = load_live_config(monkeypatch, tmp_path)
    live_envs = {name for key in live_config.LIVE_KEYS for name in key.env}
    offenders = [
        (number, name)
        for number, line in _env_inventory_rows(doc_texts[_DEPLOY_DOC])
        if "\U0001f504" in line
        for name in _ENV_RE.findall(_first_cell(line))
        if name not in live_envs
    ]

    assert offenders == [], f"{_DEPLOY_DOC} 里这些变量标了 🔄 但不是热载键: {offenders}"


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
    for rel in (_DEPLOY_DOC, "adapters/onebot/README.md"):
        for number, line in _table_rows(doc_texts[rel]):
            assert "/www/wwwroot/Clonoth" not in line, (
                f"{rel}:{number} 还把 /www/wwwroot/Clonoth 当默认工作区"
            )


def test_live_key_counts_in_deploy_doc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, doc_texts: dict[str, str],
) -> None:
    live_config = load_live_config(monkeypatch, tmp_path)
    total = len(live_config.LIVE_KEYS)
    env_backed = len([key for key in live_config.LIVE_KEYS if key.env])
    text = doc_texts[_DEPLOY_DOC]
    assert f"这 {total} 个键" in text, f"{_DEPLOY_DOC} 里的热载键总数不是 {total}"
    assert f"其中 {env_backed} 个保留了 env 兜底" in text, (
        f"{_DEPLOY_DOC} 里带 env 兜底的键数不是 {env_backed}"
    )
    assert f"另外 {total - env_backed} 个" in text, (
        f"{_DEPLOY_DOC} 里只能写 yaml 的键数不是 {total - env_backed}"
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
