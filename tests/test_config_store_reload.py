"""ConfigStore 现在只读文件、从不写回。

共享组里 config/models.yaml 是几个实例共用的一份：这边构造一次 store 就把默认值盖上去，
等于把组里所有人的渠道一起清空。读接口每次都对一下文件戳，写接口全程持文件锁，
文件写坏时停在上一份能用的配置上并拒绝一切改写。
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import config_layout  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from _lock_holder import LockHolder  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore, ConfigUnreadable  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import OpenAIConfigUpdateIn  # noqa: E402

_TOKEN = "test-config-store-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

# 手写的注释、行尾注释和自定义缩进：任何一次写回都会把它们洗掉。
HAND_WRITTEN = """\
# 这一行是人手写的，重读不该把它洗掉
version: 1
provider: openai

openai:
  base_url: https://api.example.com/v1   # 行尾注释
  api_key: sk-alpha
  model: m-alpha
  supports_vision: true
"""

BROKEN_YAML = "openai: [1, 2\n"
NOT_A_MAPPING = "- one\n- two\n"
SCHEMA_INVALID = "version: 不是数字\nprovider: openai\n"

BROKEN_FILES = pytest.mark.parametrize(
    "broken", [BROKEN_YAML, NOT_A_MAPPING, SCHEMA_INVALID], ids=["yaml", "not-a-mapping", "schema"],
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture
def models(tmp_path: Path) -> Path:
    return _write(tmp_path / "config" / "models.yaml", HAND_WRITTEN)


@pytest.fixture
def store(tmp_path: Path, models: Path) -> ConfigStore:
    return ConfigStore(path=models, workspace_root=tmp_path)


@pytest.fixture
def api(tmp_path: Path, models: Path, monkeypatch: pytest.MonkeyPatch):
    """整个 supervisor app，渠道写接口都挂在它上面。"""
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-config-store"),
            policy=PolicyEngine(workspace_root=tmp_path),
        ),
        process_manager=None,
        config_store=ConfigStore(path=models, workspace_root=tmp_path),
    )

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=_AUTH, **kwargs)

        return asyncio.run(go())

    return call


# ---------------------------------------------------------------------------
#  读：每次取值都对一下文件
# ---------------------------------------------------------------------------

class TestGettersFollowTheFile:
    def test_a_raw_edit_shows_up_on_the_next_getter(self, store: ConfigStore, models: Path) -> None:
        assert store.get_openai_secret().model == "m-alpha"

        models.write_text(HAND_WRITTEN.replace("m-alpha", "m-beta"), encoding="utf-8")

        # 没人调 reload()：控制台的原文编辑器和组里别的实例都是直接改文件。
        assert store.get_openai_secret().model == "m-beta"

    def test_another_store_on_the_same_file_is_seen(self, tmp_path: Path, models: Path) -> None:
        reader = ConfigStore(path=models, workspace_root=tmp_path)
        writer = ConfigStore(path=models, workspace_root=tmp_path)

        writer.upsert_provider("alpha", base_url="https://alpha.example", model="m-a", wire="openai")

        assert "alpha" in reader.get_providers_public()["providers"]

    def test_every_public_getter_sees_the_new_bytes(self, store: ConfigStore, models: Path) -> None:
        models.write_text(HAND_WRITTEN.replace("m-alpha", "m-beta"), encoding="utf-8")

        assert store.get_public().openai.model == "m-beta"
        assert store.get_openai_public().model == "m-beta"
        assert store.get_providers_public()["providers"]["openai"]["model"] == "m-beta"
        assert store.get_secret().openai.model == "m-beta"

    def test_a_same_size_rewrite_in_the_same_tick_is_still_noticed(
        self, store: ConfigStore, models: Path,
    ) -> None:
        # 同一刻替换成等长内容：修改时间和大小都认不出来，只有 inode 认得出。
        assert store.get_openai_secret().model == "m-alpha"
        replacement = HAND_WRITTEN.replace("m-alpha", "m-omega")
        assert len(replacement) == len(HAND_WRITTEN)
        config_layout.write_config_text(models, replacement)

        assert store.get_openai_secret().model == "m-omega"

    def test_a_deleted_file_falls_back_to_the_empty_config(self, store: ConfigStore, models: Path) -> None:
        models.unlink()

        # 文件没了不是「读坏了」：按空配置算，也不去重建它。
        assert store.load_error == ""
        assert store.get_openai_secret().model == "gpt-4o-mini"
        assert not models.exists()


# ---------------------------------------------------------------------------
#  读：一个字节都不写回
# ---------------------------------------------------------------------------

class TestReadingNeverWritesBack:
    def test_constructing_and_reloading_leave_the_bytes_alone(self, tmp_path: Path, models: Path) -> None:
        before = models.read_bytes()

        store = ConfigStore(path=models, workspace_root=tmp_path)
        store.reload()
        store.get_providers_public()

        assert models.read_bytes() == before
        assert "# 这一行是人手写的，重读不该把它洗掉" in models.read_text(encoding="utf-8")

    def test_a_missing_file_is_created_once_with_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config" / "models.yaml"

        ConfigStore(path=path, workspace_root=tmp_path)

        created = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert created["provider"] == "openai"
        assert created["openai"]["model"] == "gpt-4o-mini"

    def test_losing_the_race_to_create_it_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """base_hash="" 表示「这份还不存在」：组里别的实例抢先建好时这一笔就该让开。"""
        path = tmp_path / "config" / "models.yaml"
        real = config_layout.write_config_text

        def racing(target: Path, content: str, **kwargs: Any) -> str:
            if target == path:
                # 检查和落盘之间，别的实例先建好了它。
                real(target, "version: 1\nprovider: openai\nopenai:\n  model: theirs\n")
                raise config_layout.ConfigConflict("models.yaml 已被别处改过，刷新后再改")
            return real(target, content, **kwargs)

        monkeypatch.setattr(config_layout, "write_config_text", racing)

        store = ConfigStore(path=path, workspace_root=tmp_path)

        assert store.get_openai_secret().model == "theirs"

    def test_the_created_file_is_not_rewritten_by_a_second_store(self, tmp_path: Path) -> None:
        path = tmp_path / "config" / "models.yaml"
        ConfigStore(path=path, workspace_root=tmp_path)
        path.write_text("provider: openai\nopenai:\n  model: kept\n", encoding="utf-8")
        before = path.read_bytes()

        ConfigStore(path=path, workspace_root=tmp_path).reload()

        assert path.read_bytes() == before


# ---------------------------------------------------------------------------
#  文件写坏了
# ---------------------------------------------------------------------------

class TestABrokenFile:
    @BROKEN_FILES
    def test_the_last_good_config_stays_in_use(self, store: ConfigStore, models: Path, broken: str) -> None:
        models.write_text(broken, encoding="utf-8")

        assert store.get_openai_secret().model == "m-alpha"
        assert store.get_openai_secret().api_key == "sk-alpha"
        assert store.get_public().provider == "openai"
        assert store.get_providers_public()["providers"]["openai"]["model"] == "m-alpha"

    @BROKEN_FILES
    def test_the_load_error_says_what_is_wrong(self, store: ConfigStore, models: Path, broken: str) -> None:
        models.write_text(broken, encoding="utf-8")

        assert store.load_error

    @BROKEN_FILES
    def test_the_file_itself_is_left_alone(self, store: ConfigStore, models: Path, broken: str) -> None:
        models.write_text(broken, encoding="utf-8")
        before = models.read_bytes()

        store.reload()
        store.get_providers_public()
        with pytest.raises(ConfigUnreadable):
            store.upsert_provider("alpha", model="m", wire="openai")

        assert models.read_bytes() == before
        assert not (models.parent / f"{models.name}.bak").exists()

    @BROKEN_FILES
    @pytest.mark.parametrize("mutate", [
        pytest.param(lambda s: s.upsert_provider("alpha", model="m", wire="openai"), id="upsert_provider"),
        pytest.param(lambda s: s.delete_provider("alpha"), id="delete_provider"),
        pytest.param(lambda s: s.set_active_provider("openai"), id="set_active_provider"),
        pytest.param(lambda s: s.update_system_model("compact", model="m"), id="update_system_model"),
        pytest.param(lambda s: s.set_image_default_channel(""), id="set_image_default_channel"),
        pytest.param(lambda s: s.update_fallbacks([]), id="update_fallbacks"),
        pytest.param(lambda s: s.update_node_fallbacks("qq.orchestrator", None), id="update_node_fallbacks"),
        pytest.param(lambda s: s.update_openai(OpenAIConfigUpdateIn(model="m")), id="update_openai"),
    ])
    def test_every_mutation_is_refused(self, store: ConfigStore, models: Path, broken: str, mutate: Any) -> None:
        models.write_text(broken, encoding="utf-8")

        with pytest.raises(ConfigUnreadable) as caught:
            mutate(store)

        # ConfigUnreadable 是 ValueError 的子类，接口层的 except ValueError 才接得住。
        assert isinstance(caught.value, ValueError)
        assert "原文编辑器" in str(caught.value)

    @BROKEN_FILES
    def test_everything_recovers_once_the_file_is_fixed(
        self, store: ConfigStore, models: Path, broken: str,
    ) -> None:
        models.write_text(broken, encoding="utf-8")
        assert store.load_error

        models.write_text(HAND_WRITTEN.replace("m-alpha", "m-fixed"), encoding="utf-8")

        assert store.load_error == ""
        assert store.get_openai_secret().model == "m-fixed"
        store.upsert_provider("alpha", base_url="https://alpha.example", model="m-a", wire="openai")
        assert "alpha" in store.get_providers_public()["providers"]


class TestTheApiReportsABrokenFileAsFourHundred:
    @BROKEN_FILES
    @pytest.mark.parametrize("method,url,body", [
        ("PUT", "/v1/config/providers/openai", {"type": "openai", "model": "m"}),
        ("PUT", "/v1/config/fallbacks", {"fallbacks": []}),
        ("POST", "/v1/config/openai", {"model": "m"}),
    ])
    def test_the_write_endpoints_answer_four_hundred(
        self, api: Any, models: Path, broken: str, method: str, url: str, body: dict[str, Any],
    ) -> None:
        models.write_text(broken, encoding="utf-8")
        before = models.read_bytes()

        response = api(method, url, json=body)

        assert response.status_code == 400, response.text
        assert "原文编辑器" in response.json()["detail"]
        assert models.read_bytes() == before

    @BROKEN_FILES
    def test_the_read_endpoints_keep_serving_the_last_good_config(
        self, api: Any, models: Path, broken: str,
    ) -> None:
        models.write_text(broken, encoding="utf-8")

        response = api("GET", "/v1/config/providers")

        assert response.status_code == 200, response.text
        assert response.json()["providers"]["openai"]["model"] == "m-alpha"


# ---------------------------------------------------------------------------
#  写
# ---------------------------------------------------------------------------

class TestMutations:
    def test_the_read_happens_inside_the_file_lock(self, store: ConfigStore, models: Path) -> None:
        """锁外先读会丢掉别处刚写进去的块：拿到锁之后必须重读一遍。"""
        holder = LockHolder(config_layout._lock_path(models))
        done = threading.Event()

        def edit() -> None:
            store.upsert_provider("alpha", base_url="https://alpha.example", model="m-a", wire="openai")
            done.set()

        worker = threading.Thread(target=edit, daemon=True)
        try:
            worker.start()
            assert not done.wait(timeout=1.0), "锁被别人拿着的时候不该写进去"
            # 别处趁这会儿加了一个块。锁外读到的那份没有它。
            models.write_text(HAND_WRITTEN + "beta:\n  model: m-b\n", encoding="utf-8")
        finally:
            holder.release()
            holder.close()
        assert done.wait(timeout=30.0), "锁放开之后应当写完"
        worker.join(timeout=30.0)

        written = yaml.safe_load(models.read_text(encoding="utf-8"))
        assert written["alpha"]["model"] == "m-a"
        assert written["beta"]["model"] == "m-b"

    def test_a_no_op_delete_does_not_touch_the_file(self, store: ConfigStore, models: Path) -> None:
        before = models.read_bytes()

        store.delete_provider("ghost")

        assert models.read_bytes() == before

    @pytest.mark.parametrize("mutate", [
        pytest.param(lambda s: s.upsert_provider("openai"), id="upsert-nothing"),
        pytest.param(lambda s: s.set_active_provider("openai"), id="already-active"),
        pytest.param(lambda s: s.update_openai(OpenAIConfigUpdateIn()), id="openai-nothing"),
        pytest.param(lambda s: s.update_node_fallbacks("qq.orchestrator", None), id="chain-never-configured"),
        pytest.param(lambda s: s.set_image_default_channel(""), id="image-channel-already-empty"),
    ])
    def test_a_mutation_that_changes_nothing_writes_nothing(
        self, store: ConfigStore, models: Path, mutate: Any,
    ) -> None:
        before = models.read_bytes()

        mutate(store)

        assert models.read_bytes() == before

    def test_two_stores_editing_different_providers_keep_both(self, tmp_path: Path, models: Path) -> None:
        first = ConfigStore(path=models, workspace_root=tmp_path)
        second = ConfigStore(path=models, workspace_root=tmp_path)

        first.upsert_provider("alpha", base_url="https://alpha.example", model="m-a", wire="openai")
        second.upsert_provider("beta", base_url="https://beta.example", model="m-b", wire="openai")
        first.upsert_provider("alpha", model="m-a2")
        second.upsert_provider("beta", model="m-b2")

        written = yaml.safe_load(models.read_text(encoding="utf-8"))
        assert written["alpha"]["model"] == "m-a2"
        assert written["beta"]["model"] == "m-b2"
        assert first.get_providers_public()["providers"].keys() == second.get_providers_public()["providers"].keys()

    def test_a_delete_by_one_store_is_seen_by_the_other(self, tmp_path: Path, models: Path) -> None:
        first = ConfigStore(path=models, workspace_root=tmp_path)
        second = ConfigStore(path=models, workspace_root=tmp_path)
        first.upsert_provider("alpha", base_url="https://alpha.example", model="m-a", wire="openai")
        assert "alpha" in second.get_providers_public()["providers"]

        second.delete_provider("alpha")

        assert "alpha" not in first.get_providers_public()["providers"]

    def test_update_openai_edits_the_active_block_in_place(self, store: ConfigStore, models: Path) -> None:
        store.update_openai(OpenAIConfigUpdateIn(model="m-new"))

        written = yaml.safe_load(models.read_text(encoding="utf-8"))
        assert written["openai"]["model"] == "m-new"
        # 手写进块里的键不该被这一笔洗掉。
        assert written["openai"]["supports_vision"] is True
        assert written["openai"]["api_key"] == "sk-alpha"

    def test_update_openai_follows_the_active_channel(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "config" / "models.yaml", "\n".join([
            "version: 1",
            "provider: alpha",
            "openai:",
            "  model: m-openai",
            "alpha:",
            "  type: openai",
            "  base_url: https://alpha.example",
            "  model: m-alpha",
            "",
        ]))
        store = ConfigStore(path=path, workspace_root=tmp_path)

        store.update_openai(OpenAIConfigUpdateIn(model="m-alpha2"))

        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert written["alpha"]["model"] == "m-alpha2"
        assert written["openai"]["model"] == "m-openai"

    def test_update_openai_creates_the_missing_block_and_defaults_still_apply(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "config" / "models.yaml", "version: 1\nprovider: openai\n")
        store = ConfigStore(path=path, workspace_root=tmp_path)

        public = store.update_openai(OpenAIConfigUpdateIn(model="m-new", api_key="sk-new"))

        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        # 只写提交的那几项，没提交的不落成显式值。
        assert written["openai"] == {"api_key": "sk-new", "model": "m-new"}
        assert public.openai.base_url == "https://api.openai.com/v1"
        assert public.openai.model == "m-new"
        assert public.openai.api_key_present is True

    def test_a_refused_mutation_writes_nothing(self, store: ConfigStore, models: Path) -> None:
        before = models.read_bytes()

        with pytest.raises(ValueError):
            store.set_active_provider("ghost")

        assert models.read_bytes() == before

    @pytest.mark.parametrize("mutate", [
        pytest.param(lambda s: s.set_active_provider("fallbacks"), id="reserved-name"),
        pytest.param(lambda s: s.update_system_model("no_such_slot", model="m"), id="unknown-slot"),
        pytest.param(lambda s: s.set_image_default_channel("no_such_tool"), id="unknown-image-tool"),
        pytest.param(lambda s: s.update_node_fallbacks("  ", []), id="empty-node-id"),
        pytest.param(lambda s: s.delete_provider("openai"), id="delete-active"),
    ])
    def test_every_refusal_leaves_the_file_untouched(
        self, store: ConfigStore, models: Path, mutate: Any,
    ) -> None:
        before = models.read_bytes()

        with pytest.raises(ValueError):
            mutate(store)

        assert models.read_bytes() == before
