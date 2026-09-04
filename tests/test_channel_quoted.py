# -*- coding: utf-8 -*-
"""FeishuPlusChannel 引用消息覆写测试（不触网）。

用 ``__new__`` 绕过 ``__init__``（其需要 process / app 配置），
``_fetch_quoted_message_content`` / ``_get_user_name_by_open_id``
打桩为内存实现；``_parse_message_content`` 的非 interactive 分支
透传父类纯 JSON 解析，同样无网络。
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from qwenpaw_feishu_plus.channel import FeishuPlusChannel
from qwenpaw_feishu_plus.card.markdown import quote_block

TEST_DATA = Path(__file__).parent / "test_data"

_AT_ID = "ou_aaa0123456789abcdef0123456789aaa"


def _make_channel(
    monkeypatch: pytest.MonkeyPatch,
    fetch_result: Optional[Tuple[str, str]],
) -> Tuple[FeishuPlusChannel, Dict[str, int]]:
    """构造跳过 __init__ 的实例；fetch 计数便于断言 slash 跳过。"""
    ch = FeishuPlusChannel.__new__(FeishuPlusChannel)
    calls = {"fetch": 0}

    async def fake_fetch(parent_id: str) -> Optional[Tuple[str, str]]:
        calls["fetch"] += 1
        return fetch_result

    async def fake_name(open_id: str) -> Optional[str]:
        return "张三" if open_id == _AT_ID else None

    monkeypatch.setattr(ch, "_fetch_quoted_message_content", fake_fetch)
    monkeypatch.setattr(ch, "_get_user_name_by_open_id", fake_name)
    return ch, calls


def _mini_card_json() -> str:
    return json.dumps(
        {
            "schema": "2.0",
            "header": {"title": {"content": "告警标题"}},
            "body": {
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "content": f"**处理人:** <at id={_AT_ID}></at>",
                            "tag": "lark_md",
                        },
                    },
                ],
            },
        },
        ensure_ascii=False,
    )


# ====================================================================
# _process_quoted_message
# ====================================================================


@pytest.mark.asyncio
async def test_quoted_interactive_uses_quote_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, calls = _make_channel(
        monkeypatch,
        ("interactive", _mini_card_json()),
    )
    text_parts: List[str] = ["这个故障怎么回事"]
    content_parts: List[Any] = []

    await ch._process_quoted_message("om_demo", text_parts, content_parts)

    assert calls["fetch"] == 1  # 只 fetch 一次（不透传 super 二次请求）
    assert len(text_parts) == 2
    quoted = text_parts[0]
    # 引用块：首行 > # 标题；正文保留在引用块内；块尾空行分隔。
    assert quoted.startswith("> # 告警标题\n")
    assert "> **处理人:** @张三" in quoted
    assert quoted.endswith("\n")
    assert not quoted.startswith("[quoted")  # 不再是单行内嵌形态
    # 用户正文仍在末尾。
    assert text_parts[1] == "这个故障怎么回事"
    assert content_parts == []


@pytest.mark.asyncio
async def test_slash_command_skips_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, calls = _make_channel(
        monkeypatch,
        ("interactive", _mini_card_json()),
    )
    text_parts = ["/reset"]

    await ch._process_quoted_message("om_demo", text_parts, [])

    assert calls["fetch"] == 0
    assert text_parts == ["/reset"]


@pytest.mark.asyncio
async def test_quoted_text_uses_parent_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 interactive 保持父类拼装：[quoted message: ...]。"""
    ch, _ = _make_channel(monkeypatch, ("text", '{"text": "你好"}'))
    text_parts = ["收到"]

    await ch._process_quoted_message("om_demo", text_parts, [])

    assert text_parts[0] == "[quoted message: 你好]"
    assert text_parts[1] == "收到"


@pytest.mark.asyncio
async def test_quoted_fetch_failure_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, _ = _make_channel(monkeypatch, None)
    text_parts = ["正文"]

    await ch._process_quoted_message("om_demo", text_parts, [])

    assert text_parts == ["正文"]


@pytest.mark.asyncio
async def test_quoted_interactive_bad_json_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """interactive 但渲染失败（JSON 损坏）→ 父类单行兜底形态。"""
    ch, _ = _make_channel(monkeypatch, ("interactive", "not json"))
    text_parts = ["正文"]

    await ch._process_quoted_message("om_demo", text_parts, [])

    assert text_parts[0] == "[quoted interactive card]"
    assert text_parts[1] == "正文"


# ====================================================================
# _parse_message_content（直接消息路径）
# ====================================================================


@pytest.mark.asyncio
async def test_parse_interactive_returns_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, _ = _make_channel(monkeypatch, None)
    main_text, error_hints, content_parts = await ch._parse_message_content(
        "interactive",
        _mini_card_json(),
        "om_demo",
    )
    assert main_text is not None
    assert main_text.startswith("# 告警标题")
    assert "**处理人:** @张三" in main_text
    assert error_hints == []
    assert content_parts == []


@pytest.mark.asyncio
async def test_parse_interactive_bad_json_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, _ = _make_channel(monkeypatch, None)
    main_text, _, _ = await ch._parse_message_content(
        "interactive",
        "not json",
        "om_demo",
    )
    # 父类 extract_interactive_text 对坏 JSON 返回 None。
    assert main_text is None


@pytest.mark.asyncio
async def test_parse_text_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, _ = _make_channel(monkeypatch, None)
    main_text, _, _ = await ch._parse_message_content(
        "text",
        '{"text": "你好"}',
        "om_demo",
    )
    assert main_text == "你好"


# ====================================================================
# quote_block
# ====================================================================


def test_quote_block() -> None:
    assert quote_block("# 标题\n\n正文") == "> # 标题\n>\n> 正文\n"
    # 行内空格不误判为空行。
    assert quote_block("a") == "> a\n"
