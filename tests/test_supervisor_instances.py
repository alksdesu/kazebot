"""控制台的实例清单：单实例部署必须什么都不显示，多开时几个实例靠这份共享文件互相看见。

实例名是身份，一个实例只跑一个平台、登一个号；端口由平台描述声明，两套算法漂移会让新实例静默抢占旧实例的端口。
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from platforms.descriptor import (  # noqa: E402
    CHANNEL_GROUP,
    ChannelDescriptor,
    InstancePort,
    PlatformDescriptor,
)
from platforms.registry import register_platform, unregister_platform  # noqa: E402
from supervisor.instances import (  # noqa: E402
    MAX_INDEX,
    NoFreeIndex,
    _platform_account,
    _stored,
    account_owner,
    account_owner_in,
    allocate_index,
    instances_file,
    load_instances,
    normalize_prefix,
    port_table,
    ports_for,
    prefix_for,
    record_account,
    remove_instance,
    save_instance,
    url_prefix,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CLONOTH_URL_PREFIX", "CLONOTH_INSTANCES_FILE"):
        monkeypatch.delenv(name, raising=False)


def _write(root: Path, body: str) -> Path:
    target = root / "config" / "instances.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


@contextmanager
def _ported(*ports: InstancePort) -> Iterator[PlatformDescriptor]:
    platform = PlatformDescriptor(
        id="portcheck", display_name="端口平台", short_name="PC",
        channels=(ChannelDescriptor("portcheck_group", CHANNEL_GROUP),),
        instance_ports=ports,
    )
    register_platform(platform)
    try:
        yield platform
    finally:
        unregister_platform(platform.id)


# ── URL 前缀 ───────────────────────────────

def test_no_prefix_by_default() -> None:
    assert url_prefix() == ""


@pytest.mark.parametrize("raw", ["i/123", "/i/123", "i/123/", "  /i/123/  "])
def test_prefix_is_normalised(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_URL_PREFIX", raw)
    assert url_prefix() == "/i/123"


@pytest.mark.parametrize("blank", ["", "   ", "/"])
def test_blank_prefix_means_root(blank: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_URL_PREFIX", blank)
    assert url_prefix() == ""


def test_index_zero_owns_the_site_root() -> None:
    assert prefix_for(0, "xiaoyu") == ""
    assert prefix_for(2, "xiaoyu") == "/i/xiaoyu"


@pytest.mark.parametrize("raw,expected", [
    ("", ""), (None, ""), ("   ", ""), ("/", ""), ("//", ""), ("  /  ", ""), (0, ""),
    ("i/koki", "/i/koki"), ("/i/koki", "/i/koki"), ("/i/koki/", "/i/koki"),
    (" /i/koki ", "/i/koki"), ("i/koki/", "/i/koki"), ("///i/koki///", "/i/koki"),
    ("i", "/i"), ("/i/", "/i"),
])
def test_a_prefix_is_either_empty_or_starts_with_one_slash(raw: object, expected: str) -> None:
    # 清单里的 path、env 里的前缀、调用方传进来的都按这个比，差一个斜杠就认不出自己。
    assert normalize_prefix(raw) == expected


@pytest.mark.parametrize("raw", ["i/koki/", "  ", "/", "", "/i/koki", "///i/koki///", "i/123"])
def test_the_env_prefix_goes_through_the_same_normaliser(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_URL_PREFIX", raw)
    assert url_prefix() == normalize_prefix(raw)


def test_an_unset_env_is_the_site_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLONOTH_URL_PREFIX", raising=False)
    assert url_prefix() == "" == normalize_prefix(None)


@pytest.mark.parametrize("idx", [1, 2, MAX_INDEX])
def test_a_derived_prefix_is_already_in_normal_form(idx: int) -> None:
    # 它要和清单里读出来的 path 逐字相等，不然这个实例永远匹配不上自己那条。
    assert prefix_for(idx, "koki") == "/i/koki" == normalize_prefix(prefix_for(idx, "koki"))


@pytest.mark.parametrize("idx", [0, 1, MAX_INDEX])
def test_the_derived_prefix_survives_a_write_and_a_read(tmp_path: Path, idx: int) -> None:
    save_instance(tmp_path, instance_id="koki", label="小号", idx=idx)
    assert load_instances(tmp_path)[0]["path"] == prefix_for(idx, "koki")


# ── 端口表 ─────────────────────────────────

def test_the_table_is_the_core_port_plus_what_the_platforms_declare() -> None:
    names = {port.name: (port.base, port.step) for port in port_table()}
    assert names["supervisor"] == (8765, 10)
    # QQ 的三个端口由平台描述给出，序号 0 必须落在单实例部署原有的那一组上。
    assert {"bot": (8080, 10), "bridge": (8769, 10), "napcat": (6099, 100)}.items() <= names.items()


def test_a_platform_port_shows_up_in_every_index() -> None:
    with _ported(InstancePort("probe", 9300, 10)):
        assert ports_for(0)["probe"] == 9300
        assert ports_for(3)["probe"] == 9330


def test_index_zero_keeps_the_single_instance_ports() -> None:
    # 序号 0 必须逐字等于改造前的单实例部署，否则升级会换掉现役端口。
    assert ports_for(0) == {"supervisor": 8765, "bot": 8080, "bridge": 8769, "napcat": 6099}


def test_two_platforms_claiming_one_port_name_is_refused() -> None:
    # 重名的那个会覆盖掉另一个的端口号，两个进程就都去听同一个。
    with _ported(InstancePort("bot", 9300, 10)):
        with pytest.raises(ValueError, match="端口名重复"):
            port_table()


def test_a_port_that_collides_at_another_index_is_refused() -> None:
    # 序号 1 的 bot 就是 8090：新实例起来会把它抢走，而且只在那个序号上才犯。
    with _ported(InstancePort("probe", 8090, 5)):
        with pytest.raises(ValueError, match="撞了"):
            port_table()


# ── 序号分配 ───────────────────────────────

@pytest.mark.parametrize("idx", [True, False, 99, MAX_INDEX + 1, -1, -5, "1", 1.0, None])
def test_an_index_the_roster_cannot_use_reserves_nothing(idx: object) -> None:
    # 读侧把这些一律当成 -1，分配侧要是当真，新实例就会白跳过一个空着的序号。
    rows = [{"id": "x", "label": "x", "path": "/i/x", "idx": idx}]
    assert allocate_index(rows, probe=lambda _port: False) == 0


@pytest.mark.parametrize("idx", [0, 1, MAX_INDEX])
def test_an_index_the_roster_can_use_is_reserved(idx: int) -> None:
    rows = [{"id": "x", "label": "x", "path": prefix_for(idx, "x"), "idx": idx}]
    assert allocate_index(rows, probe=lambda _port: False) != idx


def test_running_out_of_indexes_says_so_in_words() -> None:
    with pytest.raises(NoFreeIndex, match=f"没有空闲序号了（上限 {MAX_INDEX}）"):
        allocate_index([], probe=lambda _port: True)


def test_a_roster_that_claims_every_index_runs_out_too() -> None:
    rows = [
        {"id": f"i{n}", "label": f"i{n}", "path": prefix_for(n, f"i{n}"), "idx": n}
        for n in range(MAX_INDEX + 1)
    ]
    with pytest.raises(NoFreeIndex):
        allocate_index(rows, probe=lambda _port: False)


def test_running_out_is_still_a_value_error() -> None:
    # 分出去一个自己的类型是为了和「端口表写错」那类代码错误分开，老的按 ValueError 捕的调用方不能漏。
    assert issubclass(NoFreeIndex, ValueError)
    with pytest.raises(ValueError):
        allocate_index([], probe=lambda _port: True)


# ── 清单 ───────────────────────────────────

def test_missing_file_is_a_single_instance_deployment(tmp_path: Path) -> None:
    assert load_instances(tmp_path) == []


def test_broken_yaml_does_not_raise(tmp_path: Path) -> None:
    _write(tmp_path, "instances: [ this is not: valid: yaml")
    assert load_instances(tmp_path) == []


def test_reads_entries(tmp_path: Path) -> None:
    _write(tmp_path, """
instances:
  - id: rong
    label: Rong
    path: ""
    idx: 0
    accounts:
      qq: "1000000001"
  - id: koki
    label: koki
    path: /i/koki
""")
    # idx 是后加的：根实例推得出是 0，带前缀的老条目推不出来，标 -1 交给端口探测。
    assert load_instances(tmp_path) == [
        {"id": "rong", "label": "Rong", "path": "", "idx": 0, "platform": "qq", "account": "1000000001"},
        {"id": "koki", "label": "koki", "path": "/i/koki", "idx": -1, "platform": "qq", "account": ""},
    ]


def test_label_falls_back_to_the_instance_id(tmp_path: Path) -> None:
    _write(tmp_path, "instances:\n  - id: xiaoyu\n")
    assert load_instances(tmp_path)[0]["label"] == "xiaoyu"


def test_path_is_normalised(tmp_path: Path) -> None:
    _write(tmp_path, "instances:\n  - id: xiaoyu\n    path: \"i/xiaoyu/\"\n")
    assert load_instances(tmp_path)[0]["path"] == "/i/xiaoyu"


def test_entries_without_a_name_are_dropped(tmp_path: Path) -> None:
    _write(tmp_path, "instances:\n  - label: nameless\n  - id: xiaoyu\n")
    assert [row["id"] for row in load_instances(tmp_path)] == ["xiaoyu"]


def test_duplicate_ids_keep_the_first(tmp_path: Path) -> None:
    # 重复项在切换器里是一个永远点不到的选项。
    _write(tmp_path, "instances:\n  - id: a\n    label: A\n  - id: a\n    label: B\n")
    assert [row["label"] for row in load_instances(tmp_path)] == ["A"]


def test_env_overrides_the_file_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = tmp_path / "shared.yaml"
    shared.write_text("instances:\n  - id: xiaoyu\n", encoding="utf-8")
    monkeypatch.setenv("CLONOTH_INSTANCES_FILE", str(shared))
    assert instances_file(tmp_path) == shared
    assert [row["id"] for row in load_instances(tmp_path)] == ["xiaoyu"]


# ── 清单里的平台和账号 ─────────────────────

def test_an_entry_says_which_platform_it_runs(tmp_path: Path) -> None:
    _write(tmp_path, "instances:\n  - id: yu\n    path: /i/yu\n    idx: 1\n    platform: '  QQ  '\n    account: ' 1000000002 '\n")
    assert load_instances(tmp_path)[0] == {
        "id": "yu", "label": "yu", "path": "/i/yu", "idx": 1, "platform": "qq", "account": "1000000002",
    }


def test_an_entry_without_a_platform_is_a_qq_one(tmp_path: Path) -> None:
    # 改造前没有这个字段，那时能建出来的只有 QQ 实例。
    _write(tmp_path, "instances:\n  - id: yu\n    path: /i/yu\n")
    assert load_instances(tmp_path)[0]["platform"] == "qq"
    assert load_instances(tmp_path)[0]["account"] == ""


def test_an_account_without_a_platform_lands_on_the_default_one(tmp_path: Path) -> None:
    # 手改的清单只写了账号：读回来把号丢掉最坏，冲突检查从此看不见它。
    _write(tmp_path, "instances:\n  - id: yu\n    account: '1000000002'\n")
    row = load_instances(tmp_path)[0]
    assert (row["platform"], row["account"]) == ("qq", "1000000002")


@pytest.mark.parametrize("legacy", ["uin: '1000000001'", "accounts: {qq: '1000000001'}", "accounts: {telegram: '@yu'}"])
def test_a_bare_account_wins_over_the_old_writings(tmp_path: Path, legacy: str) -> None:
    # 老字段是退回旧版时留的备份，不能反过来盖住手写的那个号。
    _write(tmp_path, f"instances:\n  - id: yu\n    account: ' 1000000002 '\n    {legacy}\n")
    row = load_instances(tmp_path)[0]
    assert (row["platform"], row["account"]) == ("qq", "1000000002")


def test_an_old_entry_is_a_qq_named_instance_logged_into_that_number(tmp_path: Path) -> None:
    # 改造前一个实例就是一个 QQ 号，实例名也是它。
    _write(tmp_path, "instances:\n  - uin: '1000000001'\n    label: 主号\n    path: ''\n")
    assert load_instances(tmp_path) == [
        {"id": "1000000001", "label": "主号", "path": "", "idx": 0, "platform": "qq", "account": "1000000001"},
    ]


def test_an_old_entry_that_switched_numbers_says_so(tmp_path: Path) -> None:
    # 实例名还是当年那个号，但现在登的是别的号：写出来的那个说了算。
    _write(tmp_path, "instances:\n  - uin: '1000000001'\n    accounts: {qq: '1000000009'}\n")
    assert load_instances(tmp_path)[0] == {
        "id": "1000000001", "label": "1000000001", "path": "", "idx": 0,
        "platform": "qq", "account": "1000000009",
    }


def test_a_declared_platform_wins_over_both_old_writings(tmp_path: Path) -> None:
    # 新字段在，老字段就是退回旧版时留的备份，不能反过来盖住现在跑的那个。
    _write(tmp_path, """
instances:
  - id: yu
    platform: telegram
    account: "@yu"
    uin: "1000000001"
    accounts: {qq: "1000000001"}
""")
    row = load_instances(tmp_path)[0]
    assert (row["platform"], row["account"]) == ("telegram", "@yu")


def test_an_old_multi_platform_entry_keeps_the_qq_number(tmp_path: Path) -> None:
    # 改造前能同时挂几个平台的只有 QQ，剩下的是那会儿的遗留。
    _write(tmp_path, "instances:\n  - id: yu\n    accounts: {telegram: '@yu', '  QQ  ': ' 1000000002 '}\n")
    row = load_instances(tmp_path)[0]
    assert (row["platform"], row["account"]) == ("qq", "1000000002")


def test_an_old_entry_without_qq_keeps_the_first_platform(tmp_path: Path) -> None:
    _write(tmp_path, "instances:\n  - id: yu\n    accounts: {telegram: '@yu', wechat: 'w-1'}\n")
    row = load_instances(tmp_path)[0]
    assert (row["platform"], row["account"]) == ("telegram", "@yu")


@pytest.mark.parametrize("body", [
    "accounts: {qq: ''}",
    "accounts: {qq: null}",
    "accounts: {'': '1000000002'}",
    "accounts: [qq, '1000000002']",
    "accounts: 1000000002",
    "account: '   '",
    "platform: '   '",
])
def test_blank_and_malformed_accounts_are_dropped(tmp_path: Path, body: str) -> None:
    _write(tmp_path, f"instances:\n  - id: xiaoyu\n    {body}\n")
    row = load_instances(tmp_path)[0]
    assert (row["platform"], row["account"]) == ("qq", "")


@pytest.mark.parametrize("entry,expected", [
    ({"platform": "  QQ  ", "account": " 1000000002 "}, ("qq", "1000000002")),
    ({"platform": "telegram"}, ("telegram", "")),
    ({"platform": "telegram", "account": None}, ("telegram", "")),
    ({"account": " 1000000002 "}, ("qq", "1000000002")),
    ({"account": "1000000002", "uin": "1000000001"}, ("qq", "1000000002")),
    ({"account": "1000000002", "accounts": {"telegram": "@yu"}}, ("qq", "1000000002")),
    ({"uin": "  1000000001  "}, ("qq", "1000000001")),
    ({"uin": ""}, ("qq", "")),
    ({"uin": None}, ("qq", "")),
    ({}, ("qq", "")),
    ({"accounts": {"qq": 0}}, ("qq", "")),
    ({"accounts": {"qq": False}}, ("qq", "")),
    ({"accounts": {None: "1000000002"}}, ("qq", "")),
    ({"accounts": [("qq", "1000000002")]}, ("qq", "")),
    # 老 uin 只在没有别的说法时才算数，映射里写出来的那个盖在它上面。
    ({"uin": "1000000001", "accounts": {"qq": "   "}}, ("qq", "1000000001")),
    ({"uin": "1000000001", "accounts": {"telegram": "@yu"}}, ("telegram", "@yu")),
])
def test_reading_a_platform_and_account_cleans_both(
    entry: dict[str, Any], expected: tuple[str, str],
) -> None:
    assert _platform_account(entry) == expected


def test_a_qq_row_also_lands_as_a_uin(tmp_path: Path) -> None:
    # 退回改造前的版本时那边只认 uin，没有它整条会被丢掉，这个实例从清单上消失。
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0, platform="qq", account="1000000001")
    assert "uin: '1000000001'" in instances_file(tmp_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("row,expected", [
    ({"platform": "qq", "account": "1000000001"}, "1000000001"),
    ({"platform": "qq", "account": ""}, None),
    ({"platform": "telegram", "account": "@yu"}, None),
])
def test_only_a_logged_in_qq_row_carries_a_uin(row: dict[str, Any], expected: str | None) -> None:
    stored = _stored({"id": "yu", "label": "yu", "path": "/i/yu", "idx": 1, **row})
    assert stored.get("uin") == expected
    assert list(stored)[:6] == ["id", "label", "path", "idx", "platform", "account"]


# ── 账号归属 ───────────────────────────────

def test_another_instance_holding_the_account_is_found(tmp_path: Path) -> None:
    _write(tmp_path, """
instances:
  - id: rong
    label: Rong
    path: ""
    accounts: {qq: "1000000001"}
  - id: koki
    label: koki
    path: /i/koki
    accounts: {qq: "1000000002"}
""")
    owner = account_owner(tmp_path, "qq", "1000000001", prefix="/i/koki")
    assert owner is not None and owner["label"] == "Rong"


def test_the_instance_you_are_on_is_not_its_own_owner(tmp_path: Path) -> None:
    # 否则控制台会拦着你重登自己已经登着的那个号。
    _write(tmp_path, "instances:\n  - id: koki\n    path: /i/koki\n    accounts: {qq: '1000000002'}\n")
    assert account_owner(tmp_path, "qq", "1000000002", prefix="/i/koki") is None


@pytest.mark.parametrize("platform_id,account", [
    ("qq", ""),
    ("qq", "1000000009"),
    ("telegram", "1000000002"),
])
def test_an_unclaimed_account_has_no_owner(tmp_path: Path, platform_id: str, account: str) -> None:
    _write(tmp_path, "instances:\n  - id: koki\n    path: /i/koki\n    accounts: {qq: '1000000002'}\n")
    assert account_owner(tmp_path, platform_id, account, prefix="") is None


@pytest.mark.parametrize("platform_id,account", [
    ("qq", " 1000000002 "),
    ("  QQ  ", "1000000002"),
    ("QQ", " 1000000002\t"),
])
def test_the_query_is_cleaned_the_same_way_the_roster_is(
    tmp_path: Path, platform_id: str, account: str,
) -> None:
    # 查询没过同一套清洗就查不到，控制台会放两个实例登同一个号。
    _write(tmp_path, "instances:\n  - id: koki\n    path: /i/koki\n    accounts: {qq: '1000000002'}\n")
    owner = account_owner(tmp_path, platform_id, account, prefix="")
    assert owner is not None and owner["id"] == "koki"


@pytest.mark.parametrize("platform_id,account", [
    ("qq", "   "),
    ("   ", "1000000002"),
    ("", ""),
])
def test_a_query_that_cleans_to_nothing_has_no_owner(tmp_path: Path, platform_id: str, account: str) -> None:
    _write(tmp_path, "instances:\n  - id: koki\n    path: /i/koki\n    accounts: {qq: '1000000002'}\n")
    assert account_owner(tmp_path, platform_id, account, prefix="") is None


def test_the_owner_is_whoever_holds_it_besides_you(tmp_path: Path) -> None:
    # 自己那条不算，剩下的才是真冲突。
    _write(tmp_path, """
instances:
  - id: rong
    label: Rong
    path: ""
    accounts: {qq: "1000000002"}
  - id: koki
    label: koki
    path: /i/koki
    accounts: {qq: "1000000002"}
""")
    for prefix, expected in (("", "koki"), ("/i/koki", "rong")):
        owner = account_owner(tmp_path, "  QQ ", " 1000000002 ", prefix=prefix)
        assert owner is not None and owner["id"] == expected


_TWO_INSTANCES = """
instances:
  - id: rong
    label: 主号
    path: ""
    accounts: {qq: "1000000001"}
  - id: koki
    label: 小号
    path: /i/koki
    accounts: {qq: "1000000002"}
"""


def _two(tmp_path: Path) -> list[dict[str, Any]]:
    _write(tmp_path, _TWO_INSTANCES)
    return load_instances(tmp_path)


def test_without_a_prefix_nobody_is_excused(tmp_path: Path) -> None:
    # 建新实例时还没有自己的前缀，谁登着都是冲突 —— 根实例也一样。
    rows = _two(tmp_path)
    for account, expected in (("1000000001", "rong"), ("1000000002", "koki")):
        owner = account_owner_in(rows, "qq", account)
        assert owner is not None and owner["id"] == expected


def test_the_root_instance_is_excused_by_an_empty_prefix(tmp_path: Path) -> None:
    # 根实例的 path 就是空串，prefix="" 说的是「我就是它」，不是「谁都算」。
    rows = _two(tmp_path)
    assert account_owner_in(rows, "qq", "1000000001", prefix="") is None
    owner = account_owner_in(rows, "qq", "1000000002", prefix="")
    assert owner is not None and owner["id"] == "koki"


def test_a_prefix_excuses_the_instance_hanging_under_it(tmp_path: Path) -> None:
    rows = _two(tmp_path)
    assert account_owner_in(rows, "qq", "1000000002", prefix="/i/koki") is None
    owner = account_owner_in(rows, "qq", "1000000001", prefix="/i/koki")
    assert owner is not None and owner["id"] == "rong"


@pytest.mark.parametrize("platform_id,account", [
    ("  QQ  ", "1000000002"),
    ("qq", " 1000000002\t"),
    ("QQ", "  1000000002  "),
])
def test_the_rows_lookup_cleans_the_query_too(tmp_path: Path, platform_id: str, account: str) -> None:
    owner = account_owner_in(_two(tmp_path), platform_id, account)
    assert owner is not None and owner["id"] == "koki"


@pytest.mark.parametrize("platform_id,account", [("qq", "   "), ("   ", "1000000002"), ("", "")])
def test_a_rows_query_that_cleans_to_nothing_has_no_owner(
    tmp_path: Path, platform_id: str, account: str,
) -> None:
    assert account_owner_in(_two(tmp_path), platform_id, account) is None


def test_an_empty_roster_owns_nothing() -> None:
    assert account_owner_in([], "qq", "1000000001") is None


@pytest.mark.parametrize("prefix", ["", "/i/koki", "/i/nobody"])
def test_both_lookups_answer_the_same(tmp_path: Path, prefix: str) -> None:
    # account_owner 就是「先读清单再查 rows」，两条路分开了就会一边拦一边放。
    rows = _two(tmp_path)
    for account in ("1000000001", "1000000002", "1000000009"):
        assert account_owner(tmp_path, "qq", account, prefix=prefix) == account_owner_in(
            rows, "qq", account, prefix=prefix,
        )


@pytest.mark.parametrize("prefix", ["/i/koki", "i/koki", "/i/koki/", " /i/koki ", "///i/koki///"])
def test_the_lookup_knows_which_one_is_you_however_the_prefix_is_written(tmp_path: Path, prefix: str) -> None:
    # 差一个斜杠就把自己当成了别人，控制台会拦着你重登自己已经登着的号。
    rows = _two(tmp_path)
    assert account_owner_in(rows, "qq", "1000000002", prefix=prefix) is None
    owner = account_owner_in(rows, "qq", "1000000001", prefix=prefix)
    assert owner is not None and owner["id"] == "rong"


@pytest.mark.parametrize("prefix", ["", "/", "  ", " / ", "//"])
def test_anything_that_cleans_to_empty_excuses_only_the_root_instance(tmp_path: Path, prefix: str) -> None:
    rows = _two(tmp_path)
    assert account_owner_in(rows, "qq", "1000000001", prefix=prefix) is None
    owner = account_owner_in(rows, "qq", "1000000002", prefix=prefix)
    assert owner is not None and owner["id"] == "koki"


def test_no_prefix_at_all_is_not_the_same_as_an_empty_one(tmp_path: Path) -> None:
    # None 是「还没有自己的前缀」，"" 是「我就是根实例」。折成一个，建新实例时就会放过根实例登着的号。
    rows = _two(tmp_path)
    assert account_owner_in(rows, "qq", "1000000001") is not None
    assert account_owner_in(rows, "qq", "1000000001", prefix="") is None


def test_the_same_number_on_another_platform_is_not_a_clash(tmp_path: Path) -> None:
    # 同机可以一边跑 QQ 一边跑别的平台，两边的号可以长得一模一样，不该互相拦着。
    _write(tmp_path, """
instances:
  - id: rong
    label: 主号
    path: ""
    platform: qq
    account: "1000000001"
  - id: koki
    label: 小号
    path: /i/koki
    platform: telegram
    account: "1000000001"
""")
    rows = load_instances(tmp_path)
    for platform_id, expected in (("qq", "rong"), ("telegram", "koki")):
        owner = account_owner_in(rows, platform_id, "1000000001")
        assert owner is not None and owner["id"] == expected
    assert account_owner_in(rows, "wechat", "1000000001") is None


# ── 清单写入 ───────────────────────────────

def test_save_sorts_by_index_and_derives_the_path(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1)
    rows = save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    assert [(row["id"], row["path"]) for row in rows] == [("rong", ""), ("koki", "/i/koki")]


def test_saving_the_same_name_twice_rewrites_instead_of_duplicating(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="rong", label="旧", idx=0)
    rows = save_instance(tmp_path, instance_id="rong", label="新", idx=0)
    assert len(rows) == 1 and rows[0]["label"] == "新"


def test_saving_defaults_to_a_qq_instance_with_nobody_logged_in(tmp_path: Path) -> None:
    rows = save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    assert (rows[0]["platform"], rows[0]["account"]) == ("qq", "")
    assert load_instances(tmp_path)[0] == rows[0]


def test_saving_keeps_the_account_already_on_record(tmp_path: Path) -> None:
    # 重存一遍不该把控制台切过的号抹掉。
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0, platform="qq", account="1000000001")
    rows = save_instance(tmp_path, instance_id="rong", label="主号", idx=0, platform="qq")
    assert rows[0]["account"] == "1000000001"


def test_switching_platform_drops_the_account_from_the_old_one(tmp_path: Path) -> None:
    # 留着的话，QQ 号会挂在一个跑 Telegram 的实例名下，冲突检查从此按错的平台拦人。
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0, platform="qq", account="1000000001")
    rows = save_instance(tmp_path, instance_id="rong", label="主号", idx=0, platform="telegram")
    assert (rows[0]["platform"], rows[0]["account"]) == ("telegram", "")
    assert load_instances(tmp_path)[0] == rows[0]


@pytest.mark.parametrize("platform,expected", [("  QQ  ", "qq"), ("", "qq"), ("   ", "qq"), (None, "qq")])
def test_saving_cleans_the_platform_id(tmp_path: Path, platform: object, expected: str) -> None:
    rows = save_instance(tmp_path, instance_id="rong", label="主号", idx=0, platform=platform)
    assert rows[0]["platform"] == expected
    assert load_instances(tmp_path)[0]["platform"] == expected


@pytest.mark.parametrize("account", ["", "   ", "\t\n", None])
def test_saving_never_writes_an_account_that_would_vanish_on_read(tmp_path: Path, account: object) -> None:
    # 写进去读不出来最坏：调用方以为记上了，冲突检查却看不见这个号。
    rows = save_instance(tmp_path, instance_id="rong", label="主号", idx=0, account=account)
    assert rows[0]["account"] == ""
    assert load_instances(tmp_path)[0]["account"] == ""


def test_saving_cleans_the_account_it_does_write(tmp_path: Path) -> None:
    rows = save_instance(tmp_path, instance_id="rong", label="主号", idx=0, account=" 1000000001 ")
    assert rows[0]["account"] == "1000000001"
    assert load_instances(tmp_path)[0]["account"] == "1000000001"


def test_a_dirty_new_account_does_not_wipe_the_one_on_record(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0, account="1000000001")
    rows = save_instance(tmp_path, instance_id="rong", label="主号", idx=0, account="   ")
    assert rows[0]["account"] == "1000000001"


@pytest.mark.parametrize("label", ["", "   ", "\t\n", None])
def test_a_label_that_is_only_whitespace_falls_back_to_the_instance_id(tmp_path: Path, label: object) -> None:
    rows = save_instance(tmp_path, instance_id="rong", label=label, idx=0)
    assert rows[0]["label"] == "rong"
    assert load_instances(tmp_path)[0]["label"] == "rong"


def test_the_label_is_stripped_before_it_is_written(tmp_path: Path) -> None:
    rows = save_instance(tmp_path, instance_id="rong", label="  主号  ", idx=0)
    assert rows[0]["label"] == "主号"
    assert load_instances(tmp_path)[0]["label"] == "主号"


@pytest.mark.parametrize("instance_id", ["", "   ", "\t\n", None])
def test_saving_without_a_name_is_refused(tmp_path: Path, instance_id: object) -> None:
    # 无名条目读回来会被整条丢掉，写的一方却以为登记上了。
    path = instances_file(tmp_path)
    with pytest.raises(ValueError, match="实例名不能为空"):
        save_instance(tmp_path, instance_id=instance_id, label="主号", idx=0)
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize("idx", [-1, -5, MAX_INDEX + 1, 99, True, False, "1", 1.0, 0.0, None])
def test_an_index_the_reader_would_not_accept_is_refused(tmp_path: Path, idx: object) -> None:
    # 越界序号读回来会被当成 -1，照写就是一条写进去和读出来对不上的记录。
    path = instances_file(tmp_path)
    with pytest.raises(ValueError, match=f"序号要在 0-{MAX_INDEX} 之间"):
        save_instance(tmp_path, instance_id="rong", label="主号", idx=idx)
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize("idx", [0, 1, MAX_INDEX])
def test_the_whole_range_is_written_and_read_back_unchanged(tmp_path: Path, idx: int) -> None:
    rows = save_instance(tmp_path, instance_id="rong", label="主号", idx=idx)
    assert rows[0]["idx"] == idx
    assert load_instances(tmp_path)[0]["idx"] == idx


@pytest.mark.parametrize("instance_id,idx", [("", 0), ("  ", 0), ("rong", -1), ("rong", MAX_INDEX + 1)])
def test_a_refused_save_does_not_touch_the_roster_it_found(
    tmp_path: Path, instance_id: object, idx: object,
) -> None:
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1, account="1000000002")
    path = instances_file(tmp_path)
    before = path.read_bytes()

    with pytest.raises(ValueError):
        save_instance(tmp_path, instance_id=instance_id, label="主号", idx=idx)
    assert path.read_bytes() == before


def test_a_padded_name_is_cleaned_before_it_lands(tmp_path: Path) -> None:
    # 前缀按实例名拼，名字没洗干净就拼出一个谁也匹配不上的 path，这个实例从此记不上号。
    rows = save_instance(tmp_path, instance_id="  rong  ", label="主号", idx=1)
    assert rows[0]["id"] == "rong" and rows[0]["path"] == "/i/rong"
    assert load_instances(tmp_path)[0] == rows[0]
    assert record_account(tmp_path, prefix="/i/rong", platform_id="qq", account="1000000001") is True
    assert load_instances(tmp_path)[0]["account"] == "1000000001"


def test_a_padded_name_is_the_same_instance(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="  rong  ", label="旧", idx=1, account="1000000001")
    rows = save_instance(tmp_path, instance_id="rong", label="新", idx=1)
    assert [(row["id"], row["path"], row["label"]) for row in rows] == [("rong", "/i/rong", "新")]
    assert rows[0]["account"] == "1000000001"


# ── 记下换上的号 ───────────────────────────

def test_recording_writes_the_new_account_onto_this_instance(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1)

    assert record_account(tmp_path, prefix="/i/koki", platform_id="qq", account="1000000002") is True

    rows = {row["id"]: (row["platform"], row["account"]) for row in load_instances(tmp_path)}
    assert rows == {"rong": ("qq", ""), "koki": ("qq", "1000000002")}


def test_recording_moves_the_row_onto_the_platform_it_reports(tmp_path: Path) -> None:
    # 清单里的平台是建实例时声明的，实际在跑的那个才作数，不然冲突检查按错的平台拦人。
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1, platform="qq", account="1000000002")
    assert record_account(tmp_path, prefix="/i/koki", platform_id="  Telegram ", account=" @koki ") is True
    row = load_instances(tmp_path)[0]
    assert (row["platform"], row["account"]) == ("telegram", "@koki")
    # 号搬走了，退回旧版用的 uin 不能留在原地。
    assert "uin" not in instances_file(tmp_path).read_text(encoding="utf-8")


def test_recording_the_same_account_on_another_platform_still_rewrites(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1, platform="qq", account="1000000002")
    assert record_account(tmp_path, prefix="/i/koki", platform_id="telegram", account="1000000002") is True
    assert load_instances(tmp_path)[0]["platform"] == "telegram"


def test_recording_the_same_account_again_does_not_rewrite(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1, account="1000000002")
    path = instances_file(tmp_path)
    before = path.read_text(encoding="utf-8")

    assert record_account(tmp_path, prefix="/i/koki", platform_id="qq", account="1000000002") is False
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("prefix", ["/i/koki", "i/koki", "/i/koki/", " /i/koki ", "///i/koki///"])
def test_recording_finds_this_instance_however_the_prefix_is_written(tmp_path: Path, prefix: str) -> None:
    # 差一个斜杠就找不到自己那条，换上的号从此记不进清单，别的实例也就拦不住双登。
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1)

    assert record_account(tmp_path, prefix=prefix, platform_id="qq", account="1000000002") is True
    assert {row["id"]: row["account"] for row in load_instances(tmp_path)} == {"rong": "", "koki": "1000000002"}


@pytest.mark.parametrize("prefix", ["", "/", "  ", " / "])
def test_recording_with_an_empty_prefix_lands_on_the_root_instance(tmp_path: Path, prefix: str) -> None:
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1)

    assert record_account(tmp_path, prefix=prefix, platform_id="qq", account="1000000001") is True
    assert {row["id"]: row["account"] for row in load_instances(tmp_path)} == {"rong": "1000000001", "koki": ""}


def test_recording_against_a_prefix_nobody_claims_does_nothing(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1)
    assert record_account(tmp_path, prefix="/i/nobody", platform_id="qq", account="1000000002") is False
    assert load_instances(tmp_path)[0]["account"] == ""


def test_recording_a_blank_account_does_nothing(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    assert record_account(tmp_path, prefix="", platform_id="qq", account="") is False
    assert load_instances(tmp_path)[0]["account"] == ""


@pytest.mark.parametrize("platform_id,account", [
    ("qq", "   "),
    ("qq", "\t\n"),
    ("qq", ""),
    ("", "1000000001"),
    ("   ", "1000000001"),
    ("", ""),
])
def test_recording_something_that_cleans_to_nothing_leaves_the_file_alone(
    tmp_path: Path, platform_id: str, account: str,
) -> None:
    # 以前纯空白会被写进去，下次读取时又消失：清单从此少一个号，冲突检查跟着漏。
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    path = instances_file(tmp_path)
    before = path.read_bytes()

    assert record_account(tmp_path, prefix="", platform_id=platform_id, account=account) is False
    assert path.read_bytes() == before
    assert load_instances(tmp_path)[0]["account"] == ""


def test_recording_strips_the_account_before_writing_it(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    assert record_account(tmp_path, prefix="", platform_id="qq", account="  1000000001  ") is True
    assert load_instances(tmp_path)[0]["account"] == "1000000001"


def test_recording_normalises_the_platform_id(tmp_path: Path) -> None:
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    assert record_account(tmp_path, prefix="", platform_id="  QQ  ", account="1000000001") is True
    assert load_instances(tmp_path)[0]["platform"] == "qq"
    assert load_instances(tmp_path)[0]["account"] == "1000000001"


@pytest.mark.parametrize("platform_id,account", [
    ("qq", "  1000000002  "),
    ("  QQ  ", "1000000002"),
    ("QQ", " 1000000002 "),
])
def test_recording_a_value_that_cleans_to_the_current_one_does_not_rewrite(
    tmp_path: Path, platform_id: str, account: str,
) -> None:
    # 清洗后再比，否则每次带空白的重登都会重写一遍清单。
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1, account="1000000002")
    path = instances_file(tmp_path)
    before = path.read_bytes()

    assert record_account(tmp_path, prefix="/i/koki", platform_id=platform_id, account=account) is False
    assert path.read_bytes() == before


def test_a_single_instance_deployment_is_not_given_a_roster_lock(tmp_path: Path) -> None:
    # 没有清单就是单实例：建出锁文件等于在别人的数据目录里凭空留下东西。
    path = instances_file(tmp_path)
    assert record_account(tmp_path, prefix="", platform_id="qq", account="1000000001") is False
    assert not path.exists()
    assert not path.with_name(f"{path.name}.lock").exists()


# ── 摘掉实例 ───────────────────────────────

def test_removing_takes_a_padded_name(tmp_path: Path) -> None:
    # 写入侧洗过名字，摘的时候不洗就摘不掉，清单里留下一个幽灵实例占着序号。
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    save_instance(tmp_path, instance_id="koki", label="小号", idx=1)
    assert [row["id"] for row in remove_instance(tmp_path, "  koki \t")] == ["rong"]
    assert [row["id"] for row in load_instances(tmp_path)] == ["rong"]


@pytest.mark.parametrize("name", ["", "   ", None, "nobody"])
def test_removing_a_name_nobody_has_keeps_everyone(tmp_path: Path, name: object) -> None:
    save_instance(tmp_path, instance_id="rong", label="主号", idx=0)
    assert [row["id"] for row in remove_instance(tmp_path, name)] == ["rong"]


# ── 读写两侧对序号的判定是同一个 ───────────

@pytest.mark.parametrize("raw,expected", [
    ("0", 0), ("1", 1), ("8", 8), ("9", -1), ("-1", -1), ("true", -1), ("'1'", -1), ("1.0", -1),
])
def test_an_index_the_writer_would_refuse_is_not_trusted_on_read(
    tmp_path: Path, raw: str, expected: int,
) -> None:
    # 手改的清单进得来，读侧放行的范围比写侧宽一格就会算出一个正在跑的实例的端口。
    _write(tmp_path, f"instances:\n  - id: koki\n    path: /i/koki\n    idx: {raw}\n")
    assert load_instances(tmp_path)[0]["idx"] == expected
