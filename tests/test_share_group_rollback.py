"""建共享组中途失败要像没建过。

建组分两步：先把组目录放到 shared/ 下，再把本实例的 config/ 换成指向它的链接。第二步失败时组目录
已经能被别的实例看见了，留在那里就是一个谁也没在用、里面却是某个实例全套配置的空组；而本实例的
config/ 还是原样，界面上却会显示它在组里。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from supervisor import share_groups  # noqa: E402


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "kazebot-data"
    root.mkdir()
    monkeypatch.setenv(config_layout.INSTANCES_FILE_ENV, str(root / "instances.yaml"))
    monkeypatch.delenv("CLONOTH_URL_PREFIX", raising=False)
    return root


@pytest.fixture
def workspace(tmp_path: Path, data_root: Path) -> Path:
    root = tmp_path / "beta"
    (root / "data").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "models.yaml").write_bytes(b"version: 1\nprovider: openai\n")
    (root / "config" / "policy.yaml").write_bytes(b"version: 1\n")
    (root / ".env").write_bytes(b"EXA_API_KEY=k-shared\nCLONOTH_PORT=8770\n")
    return root


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def _groups(data_root: Path) -> list[str]:
    shared = data_root / config_layout.SHARED_DIR
    if not shared.is_dir():
        return []
    # 组锁是共用的，不是某个组。
    return sorted(path.name for path in shared.iterdir() if not path.name.startswith("."))


class TestCreateGroupRollsBack:
    @pytest.fixture
    def failing_link(self, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        """换链接这一步炸掉。真实原因是权限不够、或者 config/ 正被别的进程占着。"""
        seen: list[Path] = []

        def boom(workspace_root: Path, target: Path) -> str:
            seen.append(Path(target))
            raise OSError("建不了链接")

        monkeypatch.setattr(share_groups, "_link_config", boom)
        return seen

    def test_the_error_reaches_the_caller(self, workspace: Path, failing_link: list[Path]) -> None:
        with pytest.raises(OSError, match="建不了链接"):
            share_groups.create_group(workspace, "alpha")

        assert failing_link, "这一步应当被走到"

    def test_the_group_directory_is_gone(
        self, workspace: Path, data_root: Path, failing_link: list[Path],
    ) -> None:
        with pytest.raises(OSError):
            share_groups.create_group(workspace, "alpha")

        # 留在那里就是一个没人用、却装着本实例全套配置的组。
        assert _groups(data_root) == []
        assert not (data_root / config_layout.SHARED_DIR / "alpha").exists()

    def test_no_staging_directory_is_left_behind(
        self, workspace: Path, data_root: Path, failing_link: list[Path],
    ) -> None:
        with pytest.raises(OSError):
            share_groups.create_group(workspace, "alpha")

        shared = data_root / config_layout.SHARED_DIR
        leftovers = [path.name for path in shared.iterdir() if path.name.endswith(".tmp")]
        assert leftovers == []

    def test_the_workspace_is_byte_for_byte_unchanged(
        self, workspace: Path, failing_link: list[Path],
    ) -> None:
        before = _snapshot(workspace)

        with pytest.raises(OSError):
            share_groups.create_group(workspace, "alpha")

        assert _snapshot(workspace) == before

    def test_the_config_directory_is_still_a_real_directory(
        self, workspace: Path, failing_link: list[Path],
    ) -> None:
        with pytest.raises(OSError):
            share_groups.create_group(workspace, "alpha")

        config = config_layout.config_dir(workspace)
        assert config.is_dir()
        assert not config_layout.is_dir_link(config)
        assert config_layout.config_share_group(workspace) is None

    def test_nothing_was_archived(self, workspace: Path, failing_link: list[Path]) -> None:
        with pytest.raises(OSError):
            share_groups.create_group(workspace, "alpha")

        archives = list((workspace / "data").glob(f"{share_groups.ARCHIVE_PREFIX}*"))
        assert archives == []

    def test_the_overview_never_mentions_the_group(
        self, workspace: Path, data_root: Path, failing_link: list[Path],
    ) -> None:
        with pytest.raises(OSError):
            share_groups.create_group(workspace, "alpha")

        overview = share_groups.overview(workspace)
        assert overview["current"] is None
        assert [group["name"] for group in overview["groups"]] == []

    def test_the_name_is_free_again_afterwards(self, workspace: Path, data_root: Path) -> None:
        """回滚不干净的话，重试会撞上「已经有叫 alpha 的共享组了」，用户再也建不出这个名字。"""
        calls: list[int] = []
        real = share_groups._link_config

        def once(workspace_root: Path, target: Path) -> str:
            calls.append(1)
            if len(calls) == 1:
                raise OSError("建不了链接")
            return real(workspace_root, target)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(share_groups, "_link_config", once)
            with pytest.raises(OSError):
                share_groups.create_group(workspace, "alpha")

            result = share_groups.create_group(workspace, "alpha")

        assert result["group"] == "alpha"
        assert config_layout.config_share_group(workspace) == "alpha"
        assert _groups(data_root) == ["alpha"]


class TestASuccessfulCreateStillWorks:
    def test_the_group_holds_the_instance_configuration(self, workspace: Path, data_root: Path) -> None:
        result = share_groups.create_group(workspace, "alpha")

        group_config = data_root / config_layout.SHARED_DIR / "alpha" / config_layout.CONFIG_DIR
        assert (group_config / "models.yaml").read_bytes() == b"version: 1\nprovider: openai\n"
        assert config_layout.config_share_group(workspace) == "alpha"
        assert result["group"] == "alpha"

    def test_only_shareable_keys_travel_with_it(self, workspace: Path, data_root: Path) -> None:
        # 端口是实例身份，抄进组里就会被别的实例拿去用。
        result = share_groups.create_group(workspace, "alpha")

        shared = config_layout.read_env_file(
            data_root / config_layout.SHARED_DIR / "alpha" / config_layout.SHARED_SECRETS_FILE,
        )
        assert dict(shared) == {"EXA_API_KEY": "k-shared"}
        assert result["copied_env"] == ["EXA_API_KEY"]
