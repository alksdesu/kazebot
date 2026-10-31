from engine.conversation_routing import is_void_terminal_text


def test_void_texts_rejected():
    for text in [
        "",
        "   ",
        "\n\t ",
        "...",
        "。。。",
        "…",
        "???",
        "！！！",
        "<YOUR_ACTUAL_RESPONSE>",
        "<RESPONSE>",
        " <FOO_BAR> ",
    ]:
        assert is_void_terminal_text(text), text


def test_real_texts_kept():
    for text in [
        "ok",
        "666",
        "好的。",
        "你好呀咪啪~",
        "T_T",
        "<3",
        "结果是 <PLACEHOLDER>",
        "a",
    ]:
        assert not is_void_terminal_text(text), text
