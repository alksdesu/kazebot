"""能力档位的纯判定。

这一层判的是「谁能按下按钮」，错一次就是把 bot 所在机器的一部分交出去，
所以每条放行与每条拒绝都单独钉住。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 直接按文件加载：这个包的 __init__ 会 get_driver()，测试进程里没有 NoneBot 驱动。
_spec = importlib.util.spec_from_file_location(
    "_qq_capability", _ROOT / "adapters" / "onebot" / "capability.py",
)
assert _spec and _spec.loader
capability = importlib.util.module_from_spec(_spec)
# dataclass 装饰器要回查自己所在的模块，先登记再执行。
sys.modules["_qq_capability"] = capability
_spec.loader.exec_module(capability)

LISTED_QQ = 10001
OWNER_QQ = 20002
GROUP_ADMIN_QQ = 20003
MEMBER_QQ = 30003
HOME_GROUP = 700001
OTHER_GROUP = 700002

GLOBAL_CAPS = ("approval", "model", "draw_preset", "custom_face", "cross_session", "send_dead_letter",
               "dream")
SCOPED_CAPS = ("clear_memory", "proactive")


def listed() -> "capability.Requester":
    return capability.Requester(user_id=LISTED_QQ, listed_admin=True)


def owner(group_id: int = HOME_GROUP) -> "capability.Requester":
    return capability.Requester(
        user_id=OWNER_QQ, listed_admin=False, group_id=group_id, group_role="owner",
    )


def group_admin(group_id: int = HOME_GROUP) -> "capability.Requester":
    return capability.Requester(
        user_id=GROUP_ADMIN_QQ, listed_admin=False, group_id=group_id, group_role="admin",
    )


def member(group_id: int = HOME_GROUP) -> "capability.Requester":
    return capability.Requester(
        user_id=MEMBER_QQ, listed_admin=False, group_id=group_id, group_role="member",
    )


class TestClamp:
    @pytest.mark.parametrize("key", GLOBAL_CAPS)
    @pytest.mark.parametrize("grant", ["owner", "group_admin"])
    def test_a_global_capability_cannot_be_handed_to_group_roles(self, key: str, grant: str) -> None:
        """改全局配置、借 bot 身份发给任意人 —— 给了一个群的群主就是给了所有群。"""
        assert capability.clamp(key, grant) == capability.LISTED

    @pytest.mark.parametrize("key", SCOPED_CAPS)
    @pytest.mark.parametrize("grant", ["owner", "group_admin"])
    def test_a_scoped_capability_keeps_what_was_configured(self, key: str, grant: str) -> None:
        assert capability.clamp(key, grant) == grant

    @pytest.mark.parametrize("key", GLOBAL_CAPS + SCOPED_CAPS)
    def test_off_survives_the_clamp(self, key: str) -> None:
        """off 比 admin 更窄，夹紧只该往窄里收，不该把它顶回 admin。"""
        assert capability.clamp(key, "off") == capability.OFF

    @pytest.mark.parametrize("grant", ["yes", "true", "owners", "", None, 1, [], "群主"])
    def test_an_unreadable_value_falls_back_to_the_default_not_the_widest(self, grant: object) -> None:
        """打错一个字不能变成提权。clear_memory 最宽能到 group_admin，回落必须是 admin。"""
        assert capability.clamp("clear_memory", grant) == capability.LISTED

    def test_case_and_padding_are_tolerated(self) -> None:
        assert capability.clamp("clear_memory", " Owner ") == capability.OWNER

    @pytest.mark.parametrize("key", GLOBAL_CAPS + SCOPED_CAPS)
    def test_a_real_boolean_false_still_reads_as_off(self, key: str) -> None:
        """dry-run 的档位从 JSON 来，`false` 到这里仍然是真布尔。不认这一层，想关掉的
        那项就按默认档放行，而且没有任何提示。"""
        assert capability.clamp(key, False) == capability.OFF

    @pytest.mark.parametrize("grant", ["no", "NO", "false", " Off "])
    def test_the_other_spellings_of_off_are_honoured(self, grant: str) -> None:
        """写 no 的意图明确，回落默认档等于把他想关的门打开。"""
        assert capability.clamp("clear_memory", grant) == capability.OFF

    def test_a_real_boolean_true_is_not_guessed_into_a_grant(self) -> None:
        """开到哪一档没有答案，只能回落默认，不能当成「最宽」。"""
        assert capability.clamp("clear_memory", True) == capability.LISTED

    def test_an_unknown_capability_reads_as_listed_only(self) -> None:
        """键名漂移时按最严的档位处理，而不是当作没有限制。"""
        assert capability.clamp("ghost", "group_admin") == capability.LISTED


class TestAllows:
    @pytest.mark.parametrize("key", GLOBAL_CAPS + SCOPED_CAPS)
    def test_the_default_grant_admits_the_roster_and_nobody_else(self, key: str) -> None:
        default = capability.BY_KEY[key].default

        assert capability.allows(key, default, listed())
        assert not capability.allows(key, default, owner())
        assert not capability.allows(key, default, group_admin())
        assert not capability.allows(key, default, member())

    def test_owner_grant_admits_the_owner_but_not_a_group_admin(self) -> None:
        assert capability.allows("clear_memory", "owner", owner())
        assert not capability.allows("clear_memory", "owner", group_admin())
        assert not capability.allows("clear_memory", "owner", member())

    def test_group_admin_grant_admits_both_group_roles(self) -> None:
        assert capability.allows("clear_memory", "group_admin", owner())
        assert capability.allows("clear_memory", "group_admin", group_admin())
        assert not capability.allows("clear_memory", "group_admin", member())

    def test_the_roster_is_admitted_by_every_grant_except_off(self) -> None:
        for grant in ("admin", "owner", "group_admin"):
            assert capability.allows("clear_memory", grant, listed())

    @pytest.mark.parametrize("key", GLOBAL_CAPS + SCOPED_CAPS)
    def test_off_shuts_out_the_roster_too(self, key: str) -> None:
        """「这项能力我不想要」是运营者的合法选择。留后门的开关不值得相信。"""
        assert not capability.allows(key, "off", listed())
        assert not capability.allows(key, "off", owner())

    def test_a_global_capability_stays_shut_even_when_the_yaml_says_owner(self) -> None:
        """夹紧在读配置时做过一次，这里再验一遍判定端 —— 两处都漏才会出事。"""
        assert not capability.allows("cross_session", "group_admin", owner())

    def test_a_group_role_means_nothing_without_a_group(self) -> None:
        """私聊没有「本群」。群主在私聊里就是个普通用户。"""
        in_private = capability.Requester(
            user_id=OWNER_QQ, listed_admin=False, group_id=None, group_role="owner",
        )

        assert not capability.allows("clear_memory", "group_admin", in_private)

    def test_an_unreported_role_is_treated_as_a_member(self) -> None:
        """有的 OneBot 实现不上报 role。缺失不能读成群主。"""
        unknown = capability.Requester(
            user_id=MEMBER_QQ, listed_admin=False, group_id=HOME_GROUP, group_role="",
        )

        assert not capability.allows("clear_memory", "group_admin", unknown)

    def test_a_made_up_role_is_treated_as_a_member(self) -> None:
        weird = capability.Requester(
            user_id=MEMBER_QQ, listed_admin=False, group_id=HOME_GROUP, group_role="superadmin",
        )

        assert not capability.allows("clear_memory", "group_admin", weird)


class TestScope:
    def test_the_owner_may_act_on_his_own_group(self) -> None:
        assert capability.scope_deny_reason("clear_memory", owner(), HOME_GROUP) == ""

    def test_the_owner_may_not_reach_another_group(self) -> None:
        """少了这一层，「把清空群记忆给群主」就变成「让群主清掉 bot 待过的每个群」。"""
        assert capability.scope_deny_reason("clear_memory", owner(), OTHER_GROUP) != ""

    def test_a_target_that_is_not_a_group_is_out_of_reach(self) -> None:
        assert capability.scope_deny_reason("proactive", owner(), None) != ""

    def test_an_unparseable_target_is_denied_rather_than_ignored(self) -> None:
        assert capability.scope_deny_reason("proactive", owner(), "群里") != ""

    def test_a_string_group_id_still_matches(self) -> None:
        """目标 id 一路上被转成过字符串，比对不能因为类型不同就判成越权。"""
        assert capability.scope_deny_reason("clear_memory", owner(), str(HOME_GROUP)) == ""

    def test_the_roster_is_not_scoped(self) -> None:
        assert capability.scope_deny_reason("clear_memory", listed(), OTHER_GROUP) == ""

    def test_a_global_capability_has_no_scope_to_clamp(self) -> None:
        """全局能力压根到不了群角色手里，作用域判定对它不该有话可说。"""
        assert capability.scope_deny_reason("model", owner(), OTHER_GROUP) == ""


class TestCatalog:
    def test_every_capability_is_published(self) -> None:
        keys = {item["key"] for item in capability.catalog()}

        assert keys == set(GLOBAL_CAPS) | set(SCOPED_CAPS)

    def test_a_global_capability_only_offers_what_it_can_honour(self) -> None:
        """界面上摆一个选了会被夹回去的档位，等于告诉运营者一个假的权限边界。"""
        entry = next(item for item in capability.catalog() if item["key"] == "model")

        assert entry["grants"] == [capability.OFF, capability.LISTED]

    def test_a_scoped_capability_offers_the_group_roles(self) -> None:
        entry = next(item for item in capability.catalog() if item["key"] == "clear_memory")

        assert entry["grants"] == [
            capability.OFF, capability.LISTED, capability.OWNER, capability.GROUP_ADMIN,
        ]

    def test_the_published_default_is_admitted_by_its_own_options(self) -> None:
        for item in capability.catalog():
            assert item["default"] in item["grants"]

    def test_every_entry_carries_something_to_read(self) -> None:
        for item in capability.catalog():
            assert item["name"] and item["desc"]

    def test_only_scoped_capabilities_are_flagged_as_scoped(self) -> None:
        flagged = {item["key"] for item in capability.catalog() if item["group_scoped"]}

        assert flagged == set(SCOPED_CAPS)


class TestRoster:
    """整份目标清单里是 bot 待过的每个群和能私聊的每个人，只有名单该看得到。"""

    def test_the_roster_may_see_it(self) -> None:
        assert capability.may_see_target_roster(listed()) is True

    def test_a_group_owner_may_not(self) -> None:
        assert capability.may_see_target_roster(owner()) is False

    def test_the_same_owner_in_a_private_chat_may_not(self) -> None:
        in_private = capability.Requester(
            user_id=OWNER_QQ, listed_admin=False, group_id=None, group_role="owner",
        )

        assert capability.may_see_target_roster(in_private) is False
