from __future__ import annotations

from toolbox.builtins.apply_diff import _apply_diff_error_response


def test_apply_diff_error_response_keeps_a_human_message_field() -> None:
    response = _apply_diff_error_response(
        "all diffs failed",
        "config/nodes/example.yaml",
        message="没有任何差异被应用",
        failedCount=2,
    )

    assert response == {
        "ok": False,
        "success": False,
        "error": "all diffs failed",
        "data": {
            "result": "ERROR: all diffs failed",
            "file": "config/nodes/example.yaml",
            "message": "没有任何差异被应用",
            "failedCount": 2,
        },
    }
