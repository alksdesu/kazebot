"""共享层的匿名别名表：编号、全平台共用的编号、按账号分的 ID、落盘格式、保留期回收、文本替换、写盘节流。"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared import aliases as alias_module  # noqa: E402
from adapters._shared.aliases import (  # noqa: E402
    AliasBook,
    AliasLoad,
    AliasNumbering,
    AliasTable,
    alias_from_index,
    alias_to_index,
)
from engine import memory_subjects  # noqa: E402
from platforms.descriptor import (  # noqa: E402
    ID_NUMERIC,
    ID_SCOPE_PLATFORM,
    ID_STRING,
    STATE_ALIASES,
    PlatformDescriptor,
)
from platforms.registry import alias_numbering_path, all_platforms, platform_state_path  # noqa: E402

_NOW = 1_800_000_000.0
_DAY = 86400.0
# 这两个 ID 全平台唯一，键不带账号；按账号分的那个用平台描述的默认值。
_NUMERIC = PlatformDescriptor(
    id="numchat", display_name="Num Chat", short_name="Num", id_shape=ID_NUMERIC, id_scope=ID_SCOPE_PLATFORM,
)
_STRING = PlatformDescriptor(
    id="strchat", display_name="Str Chat", short_name="Str", id_shape=ID_STRING, id_scope=ID_SCOPE_PLATFORM,
)
_APP = PlatformDescriptor(id="appchat", display_name="App Chat", short_name="App", id_shape=ID_STRING)
# 一份老文件名的状态文件：编号丢了重建时，得连它一起认。
_LEGACY = PlatformDescriptor(
    id="oldchat", display_name="Old Chat", short_name="Old", id_scope=ID_SCOPE_PLATFORM,
    state_files={STATE_ALIASES: "oldchat_aliases.json"},
)
_CLAIM_MANY = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from adapters._shared.aliases import AliasNumbering\n"
    "numbering = AliasNumbering(Path(sys.argv[2]), platforms=tuple)\n"
    "print(' '.join(numbering.claim('User') for _ in range(int(sys.argv[3]))))\n"
)


class _Clock:
    def __init__(self, now: float = _NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _no_moved_alias_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    # 编号丢了重建时会读各平台的别名表，开发机上设着的 env 会把它指到真文件上。
    for platform in all_platforms():
        for env in platform.state_env.values():
            monkeypatch.delenv(env, raising=False)


def _book(
    tmp_path: Path, *, shape: str = ID_NUMERIC, platform: PlatformDescriptor | None = None, **options: Any,
) -> AliasBook:
    options.setdefault("clock", _Clock())
    platform = platform or (_NUMERIC if shape == ID_NUMERIC else _STRING)
    return AliasBook(tmp_path / "data" / "aliases.json", platform=platform, workspace=tmp_path, **options)


def _platform_book(tmp_path: Path, platform: PlatformDescriptor, **options: Any) -> AliasBook:
    """放在平台自己的状态文件位置上的别名表，和适配器里的一样。"""
    options.setdefault("clock", _Clock())
    return AliasBook(platform_state_path(platform, STATE_ALIASES, tmp_path), platform=platform, workspace=tmp_path, **options)


def _numbering_file(tmp_path: Path) -> dict[str, Any]:
    return json.loads(alias_numbering_path(tmp_path).read_text(encoding="utf-8"))


def _write(tmp_path: Path, payload: Any) -> None:
    path = tmp_path / "data" / "aliases.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


class TestNumbering:
    @pytest.mark.parametrize(
        ("index", "alias"),
        [(0, "UserA"), (25, "UserZ"), (26, "UserAA"), (51, "UserAZ"), (52, "UserBA"), (701, "UserZZ"), (702, "UserAAA")],
    )
    def test_letters_run_like_spreadsheet_columns(self, index: int, alias: str) -> None:
        assert alias_from_index("User", index) == alias
        assert alias_to_index("User", alias) == index

    @pytest.mark.parametrize("alias", ["User", "Userab", "User1", "UserA1", "GroupA"])
    def test_anything_else_is_not_an_index(self, alias: str) -> None:
        assert alias_to_index("User", alias) == -1

    def test_negative_indexes_clamp_to_the_first_alias(self) -> None:
        assert alias_from_index("Group", -5) == "GroupA"


class TestTable:
    def test_aliases_are_minted_once_and_resolve_back(self) -> None:
        table = AliasTable("User")

        assert table.assign("10001") == ("UserA", True)
        assert table.assign("10002") == ("UserB", True)
        assert table.assign("10001") == ("UserA", False)
        assert table.real_of("UserB") == "10002"
        assert "10001" in table and len(table) == 2

    def test_touching_is_throttled(self) -> None:
        table = AliasTable("User")
        table.assign("10001")

        assert table.touch("10001", _NOW) is True
        assert table.touch("10001", _NOW + 60) is False
        assert table.touch("10001", _NOW + alias_module.TOUCH_MIN_INTERVAL + 1) is True

    @pytest.mark.parametrize(
        ("persisted", "expected"),
        [(None, 5), (3, 5), (9, 9), ("7", 7), ("junk", 5), (-4, 5), (float("inf"), 5)],
    )
    def test_numbering_resumes_past_every_used_alias(self, persisted: Any, expected: int) -> None:
        table = AliasTable("User")
        table.restore("10001", "UserA", _NOW)
        table.restore("10002", "UserE", _NOW)

        table.resume_numbering(persisted)

        assert table.next_index == expected

    def test_a_pruned_alias_is_never_handed_out_again(self) -> None:
        table = AliasTable("User")
        table.restore("10001", "UserA", _NOW)
        table.restore("10002", "UserB", _NOW - 40 * _DAY)
        table.resume_numbering(None)

        assert table.prune(_NOW - 30 * _DAY, frozenset()) == 1
        assert table.real_of("UserB") is None
        assert table.assign("10003") == ("UserC", True)

    def test_protected_aliases_survive_pruning(self) -> None:
        table = AliasTable("User")
        table.restore("10001", "UserA", _NOW - 40 * _DAY)

        assert table.prune(_NOW - 30 * _DAY, frozenset({"UserA"})) == 0
        assert table.real_of("UserA") == "10001"


class TestBook:
    def test_empty_ids_get_the_unknown_alias_without_touching_the_file(self, tmp_path: Path) -> None:
        book = _book(tmp_path)

        assert book.user_alias("  ") == "UserUnknown"
        assert book.group_alias("") == "GroupUnknown"
        assert not book.dirty

    def test_a_new_alias_marks_the_book_dirty(self, tmp_path: Path) -> None:
        book = _book(tmp_path)

        assert book.user_alias(" 10001 ") == "UserA"
        assert book.dirty

    def test_the_file_format_is_unchanged(self, tmp_path: Path) -> None:
        # supervisor 的会话标注直接读这份文件，字段名和嵌套都不能动。
        book = _book(tmp_path)
        book.user_alias("10001")
        book.group_alias("20001")

        asyncio.run(book.save())

        data = json.loads(book.path.read_text(encoding="utf-8"))
        assert set(data) == {"version", "updated_at", "user_next", "group_next", "users", "groups"}
        assert data["version"] == 1
        assert (data["user_next"], data["group_next"]) == (1, 1)
        assert data["users"] == {"10001": {"alias": "UserA", "last_seen": _NOW}}
        assert data["groups"] == {"20001": {"alias": "GroupA", "last_seen": _NOW}}
        assert not book.dirty

    def test_a_clean_book_does_not_write(self, tmp_path: Path) -> None:
        book = _book(tmp_path)

        asyncio.run(book.save())

        assert not book.path.exists()

    def test_a_round_trip_restores_aliases_and_numbering(self, tmp_path: Path) -> None:
        first = _book(tmp_path)
        first.user_alias("10001")
        first.user_alias("10002")
        first.group_alias("20001")
        asyncio.run(first.save())

        second = _book(tmp_path)

        assert second.load() == AliasLoad(users=2, groups=1, pruned=0, rejected=0)
        assert second.users.real_of("UserB") == "10002"
        assert second.user_alias("10003") == "UserC"
        assert second.substitute("10002") == "UserB"

    def test_rejected_users_are_dropped_and_the_file_is_rewritten(self, tmp_path: Path) -> None:
        _write(tmp_path, {"users": {"1": {"alias": "UserA"}, "10002": {"alias": "UserB"}}, "groups": {}})
        book = _book(tmp_path)

        loaded = book.load(accept_user=lambda real: len(real) >= 5)

        assert loaded is not None and loaded.rejected == 1
        assert "1" not in book.users and book.users.real_of("UserB") == "10002"
        assert book.dirty

    def test_a_missing_file_loads_nothing(self, tmp_path: Path) -> None:
        assert _book(tmp_path).load() is None

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
    def test_an_unreadable_file_loads_nothing(self, tmp_path: Path, content: str) -> None:
        _write(tmp_path, content)
        book = _book(tmp_path)

        assert book.load() is None
        assert len(book.users) == 0

    def test_malformed_entries_are_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path, {
            "users": {
                "": {"alias": "UserA"},
                "10001": "UserB",
                "10002": {"alias": ""},
                "10003": {"alias": "UserD", "last_seen": "junk"},
            },
            "groups": [],
        })
        book = _book(tmp_path)

        loaded = book.load()

        assert loaded == AliasLoad(users=1, groups=0, pruned=0, rejected=0)
        # 读不出的最后出现时间按当前时间算，否则刚加载就会被保留期判成过期。
        assert book.users.last_seen("10003") == _NOW
        assert book.users.next_index == 4

    def test_retention_prunes_only_after_numbering_is_restored(self, tmp_path: Path) -> None:
        # 旧文件没有 user_next：先回收再恢复编号的话，新人会拿到被回收的 UserE。
        _write(tmp_path, {
            "users": {
                "10001": {"alias": "UserA", "last_seen": _NOW},
                "10002": {"alias": "UserE", "last_seen": _NOW - 400 * _DAY},
            },
            "groups": {},
        })
        book = _book(tmp_path, retention_days=30)

        loaded = book.load()

        assert loaded is not None and loaded.pruned == 1
        assert book.user_alias("10003") == "UserF"

    def test_enrolled_subjects_are_never_pruned(self, tmp_path: Path) -> None:
        memory_subjects.record_interaction(tmp_path, "UserA")
        _write(tmp_path, {"users": {"10001": {"alias": "UserA", "last_seen": _NOW - 400 * _DAY}}, "groups": {}})
        book = _book(tmp_path, retention_days=30)

        loaded = book.load()

        assert loaded is not None and loaded.pruned == 0
        assert book.users.real_of("UserA") == "10001"


class TestSharedNumbering:
    def test_two_platforms_never_hand_out_the_same_alias(self, tmp_path: Path) -> None:
        # 记忆按别名建目录、跨平台共享：两个平台各发一个 UserA，就是两个人共用一份记忆。
        numeric, string = _platform_book(tmp_path, _NUMERIC), _platform_book(tmp_path, _STRING)

        issued = [numeric.user_alias("10001"), string.user_alias("E2B7C1A0"), numeric.user_alias("10002")]

        assert issued == ["UserA", "UserB", "UserC"]
        assert numeric.group_alias("20001") == "GroupA" and string.group_alias("room-1") == "GroupB"
        assert _numbering_file(tmp_path)["next"] == {"Group": 2, "User": 3}
        # 文件里存的下一个编号跟着领到的走：重建编号、老版本回退都靠它。
        assert (numeric.users.next_index, string.users.next_index) == (3, 2)

    def test_loading_reserves_what_the_file_already_uses(self, tmp_path: Path) -> None:
        # 编号文件落后于某份别名表（从备份恢复过、或别的进程重建时没看见它）：加载那一刻就补齐。
        alias_numbering_path(tmp_path).parent.mkdir(parents=True)
        alias_numbering_path(tmp_path).write_text(json.dumps({"next": {"User": 0}}), encoding="utf-8")
        numeric = _platform_book(tmp_path, _NUMERIC)
        numeric.path.parent.mkdir(parents=True, exist_ok=True)
        numeric.path.write_text(
            json.dumps({"user_next": 5, "users": {"10001": {"alias": "UserC", "last_seen": _NOW}}}), encoding="utf-8",
        )

        numeric.load()

        assert _platform_book(tmp_path, _STRING).user_alias("E2B7C1A0") == "UserF"

    def test_a_missing_numbering_counts_alias_maps_kept_under_legacy_names(self, tmp_path: Path) -> None:
        legacy = platform_state_path(_LEGACY, STATE_ALIASES, tmp_path)
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"users": {"1": {"alias": "UserD"}}, "group_next": 4}), encoding="utf-8")
        numbering = AliasNumbering(tmp_path, platforms=lambda: (_LEGACY,))

        assert (numbering.claim("User"), numbering.claim("Group")) == ("UserE", "GroupE")
        assert _numbering_file(tmp_path)["next"] == {"Group": 5, "User": 5}

    def test_a_missing_numbering_counts_platforms_that_are_not_registered_now(self, tmp_path: Path) -> None:
        # 这个平台这次没起，数据目录里的标准文件照样要认，它的人还在记忆里。
        unlisted = tmp_path / "data" / "platforms" / "gonechat" / "aliases.json"
        unlisted.parent.mkdir(parents=True)
        unlisted.write_text(json.dumps({"user_next": 7, "groups": {"g": {"alias": "GroupH"}}}), encoding="utf-8")
        numbering = AliasNumbering(tmp_path, platforms=tuple)

        assert (numbering.claim("User"), numbering.claim("Group")) == ("UserH", "GroupI")

    def test_a_missing_numbering_counts_the_enrolled_people(self, tmp_path: Path) -> None:
        # 别名表丢了，建档的人记忆还在，他的别名不能再发给别人。
        memory_subjects.record_interaction(tmp_path, "UserK")

        assert AliasNumbering(tmp_path, platforms=tuple).claim("User") == "UserL"

    def test_a_rebuilt_numbering_is_written_down_at_once(self, tmp_path: Path) -> None:
        # 不落盘的话，之后每次领编号都得把所有别名文件重扫一遍。
        memory_subjects.record_interaction(tmp_path, "UserK")

        AliasNumbering(tmp_path, platforms=tuple).reserve("User", 1)

        assert _numbering_file(tmp_path)["next"] == {"User": 11}

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"next": [3]}'])
    def test_a_broken_numbering_is_rebuilt(self, tmp_path: Path, content: str) -> None:
        numeric = _platform_book(tmp_path, _NUMERIC)
        numeric.path.parent.mkdir(parents=True, exist_ok=True)
        numeric.path.write_text(json.dumps({"users": {"10001": {"alias": "UserC"}}}), encoding="utf-8")
        alias_numbering_path(tmp_path).write_text(content, encoding="utf-8")

        assert AliasNumbering(tmp_path, platforms=lambda: (_NUMERIC,)).claim("User") == "UserD"
        assert _numbering_file(tmp_path)["next"]["User"] == 4

    def test_a_table_skips_aliases_it_already_holds(self, tmp_path: Path) -> None:
        # 编号文件被删掉重建时，本进程还没落盘的新别名不在任何一份文件里。
        book = _platform_book(tmp_path, _NUMERIC)
        book.users.restore("10001", "UserA", _NOW)

        assert book.user_alias("10002") == "UserB"

    def test_reserving_never_moves_the_numbering_back(self, tmp_path: Path) -> None:
        numbering = AliasNumbering(tmp_path, platforms=tuple)
        numbering.reserve("User", 6)
        numbering.reserve("User", 2)
        numbering.reserve("User", 0)

        assert numbering.claim("User") == "UserG"

    def test_platforms_claiming_at_once_never_collide(self, tmp_path: Path) -> None:
        # 每个平台各一个进程：读改写不跨进程加锁，两个进程会领到同一个编号。
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", _CLAIM_MANY, str(_ROOT), str(tmp_path), "20"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            )
            for _ in range(4)
        ]
        claimed: list[str] = []
        for worker in workers:
            out, err = worker.communicate(timeout=120)
            assert worker.returncode == 0, err
            claimed.extend(out.split())

        assert sorted(claimed, key=lambda alias: alias_to_index("User", alias)) == [
            alias_from_index("User", index) for index in range(80)
        ]


class TestAccountScopedIds:
    def test_keys_carry_the_bot_account(self, tmp_path: Path) -> None:
        book = _platform_book(tmp_path, _APP)
        book.bind_account("app-1")
        book.user_alias("u1")
        book.group_alias("g1")

        asyncio.run(book.save())

        data = json.loads(book.path.read_text(encoding="utf-8"))
        assert data["users"] == {"app-1/u1": {"alias": "UserA", "last_seen": _NOW}}
        assert data["groups"] == {"app-1/g1": {"alias": "GroupA", "last_seen": _NOW}}
        assert book.identity("u1") == "app-1/u1"

    def test_a_book_that_never_learned_its_account_refuses_to_key_ids(self, tmp_path: Path) -> None:
        book = _platform_book(tmp_path, _APP)

        with pytest.raises(ValueError, match="bot account"):
            book.user_alias("u1")
        assert book.user_alias("") == "UserUnknown"

    def test_the_same_id_under_two_accounts_is_two_people(self, tmp_path: Path) -> None:
        book = _platform_book(tmp_path, _APP)
        book.bind_account("app-1")
        first = book.user_alias("u1")
        book.bind_account("app-2")

        assert (first, book.user_alias("u1")) == ("UserA", "UserB")

    def test_text_and_reverse_lookups_only_see_the_current_account(self, tmp_path: Path) -> None:
        book = _platform_book(tmp_path, _APP)
        book.bind_account("app-1")
        book.user_alias("u1")
        book.substitute("warm the cache")
        book.bind_account("app-2")

        # 另一个号名下的 ID 在这个号上不会出现，反查出来也 @ 不到人。
        assert book.substitute("u1 说好") == "u1 说好"
        assert book.user_real_of("UserA") is None
        assert book.user_alias_of("u1") is None

        book.bind_account("app-1")

        assert book.substitute("u1 说好") == "UserA 说好"
        assert book.user_real_of("UserA") == "u1"
        assert book.user_alias_of("u1") == "UserA"

    def test_a_key_without_an_account_belongs_to_nobody(self, tmp_path: Path) -> None:
        book = _platform_book(tmp_path, _APP)
        book.path.parent.mkdir(parents=True)
        book.path.write_text(json.dumps({"users": {"u1": {"alias": "UserA"}}}), encoding="utf-8")

        book.load()

        assert book.user_real_of("UserA") is None
        book.bind_account("app-1")
        assert book.user_real_of("UserA") is None and book.substitute("u1") == "u1"

    def test_the_user_filter_judges_the_raw_id(self, tmp_path: Path) -> None:
        book = _platform_book(tmp_path, _APP)
        book.path.parent.mkdir(parents=True)
        book.path.write_text(
            json.dumps({"users": {"app-1/12": {"alias": "UserA"}, "app-1/u2": {"alias": "UserB"}}}), encoding="utf-8",
        )
        seen: list[str] = []

        loaded = book.load(accept_user=lambda raw: seen.append(raw) or raw != "12")

        assert loaded is not None and loaded.rejected == 1
        assert sorted(seen) == ["12", "u2"]

    def test_platform_wide_ids_ignore_the_account(self, tmp_path: Path) -> None:
        book = _platform_book(tmp_path, _NUMERIC)
        book.bind_account("90001")
        first = book.user_alias("10001")
        book.bind_account("90002")

        assert book.user_alias("10001") == first
        assert book.user_real_of(first) == "10001" and book.identity("10001") == "10001"

    def test_looking_up_an_alias_does_not_hand_one_out(self, tmp_path: Path) -> None:
        book = _book(tmp_path)

        assert book.user_alias_of("10001") is None and book.group_alias_of("20001") is None
        assert book.user_alias_of("") is None
        assert not book.dirty and len(book.users) == 0
        assert book.user_real_of("UserZ") is None and book.group_real_of("GroupZ") is None


class TestSubstitution:
    def test_known_numeric_ids_are_replaced_on_digit_boundaries(self, tmp_path: Path) -> None:
        book = _book(tmp_path)
        book.user_alias("123456")
        book.user_alias("12345")

        assert book.substitute("123456 和 12345，还有 1234567，金额 12345元") == "UserA 和 UserB，还有 1234567，金额 UserB元"

    def test_groups_win_when_a_user_and_a_group_share_an_id(self, tmp_path: Path) -> None:
        book = _book(tmp_path)
        book.user_alias("555555")
        book.group_alias("555555")

        assert book.substitute("555555") == "GroupA"

    def test_the_cached_pattern_follows_new_aliases(self, tmp_path: Path) -> None:
        book = _book(tmp_path)
        book.user_alias("100001")
        assert book.substitute("100002") == "100002"

        book.user_alias("100002")

        assert book.substitute("100002") == "UserB"

    def test_string_ids_stop_at_word_characters_but_not_at_at_signs(self, tmp_path: Path) -> None:
        book = _book(tmp_path, shape=ID_STRING)
        book.user_alias("E2B7C1A0")

        assert book.substitute("@E2B7C1A0 说") == "@UserA 说"
        assert book.substitute("xE2B7C1A0 E2B7C1A0x") == "xE2B7C1A0 E2B7C1A0x"

    def test_nothing_known_leaves_text_alone(self, tmp_path: Path) -> None:
        book = _book(tmp_path)

        assert book.substitute("") == ""
        assert book.substitute("12345") == "12345"


class TestWriteBehind:
    def test_changes_inside_one_window_are_written_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        book = _book(tmp_path, save_min_interval=0.05)
        writes: list[int] = []
        save = AliasBook.save

        async def counting_save(self: AliasBook) -> None:
            writes.append(1)
            await save(self)

        monkeypatch.setattr(AliasBook, "save", counting_save)

        async def scenario() -> None:
            book.user_alias("10001")
            book.user_alias("10002")
            book.group_alias("20001")
            assert book.save_pending
            await asyncio.sleep(0.3)

        asyncio.run(scenario())

        assert writes == [1]
        assert json.loads(book.path.read_text(encoding="utf-8"))["user_next"] == 2
        assert not book.save_pending

    def test_flush_writes_at_once_and_cancels_the_waiting_write(self, tmp_path: Path) -> None:
        book = _book(tmp_path, save_min_interval=3600)

        async def scenario() -> None:
            book.user_alias("10001")
            assert book.save_pending
            await book.flush()
            assert not book.save_pending

        asyncio.run(scenario())

        assert json.loads(book.path.read_text(encoding="utf-8"))["users"]["10001"]["alias"] == "UserA"

    def test_without_an_event_loop_the_book_waits_for_an_explicit_save(self, tmp_path: Path) -> None:
        book = _book(tmp_path)

        book.user_alias("10001")

        assert book.dirty and not book.save_pending

    def test_a_failed_write_keeps_the_book_dirty(self, tmp_path: Path) -> None:
        target = tmp_path / "data" / "aliases.json"
        target.mkdir(parents=True)
        book = _book(tmp_path)
        book.user_alias("10001")

        asyncio.run(book.save())

        assert book.dirty
