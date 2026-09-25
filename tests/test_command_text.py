from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException

from clonoth_sdk.command_text import canonicalize_quiet_command, parse_duration_seconds
from supervisor.community.commands import command
from supervisor.community.service import CommunityService
from supervisor.feature_auth import FeatureActor


@pytest.mark.parametrize("text,seconds", [
    ("半小时", 1800), ("半个小时", 1800), ("一个半小时", 5400), ("一小时半", 5400),
    ("一小时三十分钟", 5400), ("一小时三分钟半", 3810), ("一刻钟", 900), ("一刻", 900), ("十五分钟", 900),
    ("两分钟", 120), ("十分钟", 600), ("二十分钟", 1200), ("一百二十秒", 120),
    ("一百零五秒", 105), ("一千零五秒", 1005), ("半天", 43200), ("一天半", 129600),
    ("七天", 604800), ("0.5小时", 1800), ("30分钟", 1800), ("1小时30分15秒", 5415),
    ("一 小时 三十 分钟", 5400), ("零秒", 0), ("0秒", 0), ("30秒钟", 30),
])
def test_duration_parser_returns_seconds(text, seconds):
    assert parse_duration_seconds(text) == seconds


@pytest.mark.parametrize("text", [
    "", "过半小时", "半小时后", "明天", "一百五分钟", "一二分钟", "十百分钟",
    "半半小时", "一半小时", "1.5个半小时", "-1小时", "+1小时", "1e3秒", "nan小时",
    "1小时1天", "1小时1小时", "三十", "1小时然后恢复", "\"半小时\"", "一小时半分钟半",
    "1个分钟", "9" * 129 + "小时", "半小时恢复文件",
])
def test_ambiguous_or_partial_duration_is_not_interpreted(text):
    assert parse_duration_seconds(text) is None


@pytest.mark.parametrize("text,expected", [
    ("恢复", "/恢复"), (" 恢复！ ", "/恢复"), ("／恢复", "/恢复"),
    ("安静半小时", "/安静 1800秒"), ("旁听 半个小时。", "/旁听 1800秒"),
    ("安静一个半小时", "/安静 5400秒"), ("/安静 半小时", "/安静 1800秒"),
    ("／旁听 十五分钟", "/旁听 900秒"), ("安静状态", "/安静状态"),
    ("/安静", "/安静"), ("/安静 30", "/安静 30"),
])
def test_quiet_command_has_one_canonical_form(text, expected):
    assert canonicalize_quiet_command(text) == expected


@pytest.mark.parametrize("text", [
    "恢复文件", "我恢复了", "身体恢复", "别恢复", "他说安静半小时", "能恢复吗？",
    "安静半小时后恢复文件", "恢复/删除文件", "安静", "旁听", "安静一百五分钟",
])
def test_normal_conversation_is_not_a_quiet_control(text):
    assert canonicalize_quiet_command(text) == text


def test_natural_quiet_and_restore_use_the_same_service_permissions_and_deadline(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("supervisor.community.service.time.time", lambda: now[0])
    service = CommunityService(tmp_path)
    manager = FeatureActor(scope="qq_group:a", owner="manager", role="admin", channel="qq_group")
    result = command(service, manager, {"text": "安静半小时"})
    assert result["control"]
    assert service.state(manager)["quiet"]["expires_at"] == 2800
    assert service.state(replace(manager, scope="qq_group:b"))["quiet"] == {}
    with pytest.raises(HTTPException):
        command(service, replace(manager, role="member", owner="member"), {"text": "恢复"})
    assert service.state(manager)["quiet"]["mode"] == "silent"
    assert command(service, manager, {"text": "恢复文件"}) is None
    assert command(service, manager, {"text": "我恢复了"}) is None
    assert command(service, manager, {"text": "恢复"})["control"]
    assert service.state(manager)["quiet"] == {}
    command(service, manager, {"text": "旁听十五分钟"})
    now[0] += 901
    assert service.state(manager)["quiet"] == {}


@pytest.mark.parametrize("text", ["安静零秒", "安静八天", "/安静 -1秒"])
def test_natural_duration_still_obeys_service_bounds(tmp_path, text):
    service = CommunityService(tmp_path)
    manager = FeatureActor(scope="qq_group:a", owner="manager", role="admin", channel="qq_group")
    with pytest.raises(ValueError):
        command(service, manager, {"text": text})
    assert service.state(manager)["quiet"] == {}
