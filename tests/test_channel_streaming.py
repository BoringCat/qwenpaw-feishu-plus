# -*- coding: utf-8 -*-
"""FeishuPlusChannel 命令消息跳过流式输出测试（不触网）。

流式输出（CardKit 卡片）只在 ``streaming_enabled`` 时启用，入口为
``_before_consume_process``（预创建卡片）与 ``on_streaming_start``
（创建/复用卡片）。以 ``/`` 开头的命令消息在这两处都应跳过 ——
``_before_consume_process`` 置 ``_NO_STREAMING_REQUEST_ATTR`` 标记并
跳过预创建，``on_streaming_start`` 读到标记直接返回，结果由父类
``on_streaming_end`` 以纯文本回退发送。

用 ``__new__`` 绕过 ``__init__``，涉及父类的 IO / 卡片方法全部以
record 型 stub 替换，只验证跳过行为本身。
"""
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from qwenpaw_feishu_plus.channel import (
    FeishuPlusChannel,
    _NO_STREAMING_REQUEST_ATTR,
)


def _make_channel() -> FeishuPlusChannel:
    """构造跳过 __init__ 的实例（默认开启流式输出）。"""
    ch = FeishuPlusChannel.__new__(FeishuPlusChannel)
    ch.streaming_enabled = True
    return ch


def _text_part(text: str) -> Any:
    """与父类 TextContent(type=text) 结构等价的文本 content part。"""
    return SimpleNamespace(type="text", text=text)


def _make_request(text: str) -> Any:
    """构造带用户正文的最小 AgentRequest 等价物。

    ``input[0].content`` 首段即用户 query（父类 _on_message 已剥离
    mention key），_extract_query_from_payload 据此提取；channel_meta /
    session_id 供 _save_receive_id 与 _before_consume_process 使用。
    """
    return SimpleNamespace(
        input=[SimpleNamespace(content=[_text_part(text)])],
        channel_meta={
            "feishu_receive_id": "oc_demo",
            "feishu_receive_id_type": "chat_id",
        },
        session_id="oc_demo",
    )


# ====================================================================
# _request_is_slash_command
# ====================================================================


def test_request_is_slash_command_true() -> None:
    ch = _make_channel()
    assert ch._request_is_slash_command(_make_request("/clear")) is True
    # 前置空格 / 大小写不受影响。
    assert ch._request_is_slash_command(_make_request("  /New 汇总")) is True


def test_request_is_slash_command_false() -> None:
    ch = _make_channel()
    assert ch._request_is_slash_command(_make_request("正常提问")) is False
    assert ch._request_is_slash_command(_make_request("斜杠 / 在句中")) is False


def test_request_is_slash_command_empty_input() -> None:
    ch = _make_channel()
    # 无 input / 空 input / 无文本 part → 恒 False。
    req = SimpleNamespace(input=None)
    assert ch._request_is_slash_command(req) is False
    req.input = []
    assert ch._request_is_slash_command(req) is False


# ====================================================================
# _before_consume_process —— 命令消息跳过流式卡片预创建
# ====================================================================


async def test_before_consume_slash_skips_precreate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _make_channel()
    calls: Dict[str, int] = {"create": 0}

    async def fake_create(*args: Any, **kwargs: Any) -> Dict[str, str]:
        calls["create"] += 1
        return {"card_id": "cv_demo", "message_id": "om_demo"}

    async def fake_save_receive_id(*args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr(ch, "_create_streaming_card", fake_create)
    monkeypatch.setattr(ch, "_save_receive_id", fake_save_receive_id)

    req = _make_request("/new 一句话总结本期重点")
    await ch._before_consume_process(req)

    # 命令消息：不预创建卡片，仅打跳过流式标记。
    assert calls["create"] == 0
    assert getattr(req, _NO_STREAMING_REQUEST_ATTR, False) is True
    assert not hasattr(req, "_precreated_card")


async def test_before_consume_normal_precreates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _make_channel()
    calls: Dict[str, int] = {"create": 0}

    async def fake_create(*args: Any, **kwargs: Any) -> Dict[str, str]:
        calls["create"] += 1
        return {"card_id": "cv_demo", "message_id": "om_demo"}

    async def fake_save_receive_id(*args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr(ch, "_create_streaming_card", fake_create)
    monkeypatch.setattr(ch, "_save_receive_id", fake_save_receive_id)

    req = _make_request("正常对话")
    await ch._before_consume_process(req)

    # 普通消息：照常预创建卡片、复用给 on_streaming_start，不打标记。
    assert calls["create"] == 1
    assert getattr(req, _NO_STREAMING_REQUEST_ATTR, False) is False
    assert req._precreated_card == {
        "card_id": "cv_demo",
        "message_id": "om_demo",
    }


# ====================================================================
# on_streaming_start —— 命令消息不创建流式卡片
# ====================================================================


async def test_on_streaming_start_slash_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _make_channel()
    recv_calls: Dict[str, int] = {"recv": 0}

    async def fake_get_receive(*args: Any, **kwargs: Any) -> Any:
        recv_calls["recv"] += 1
        return ("open_id", "ou_demo")

    monkeypatch.setattr(ch, "_get_receive_for_send", fake_get_receive)

    req = _make_request("/help")
    setattr(req, _NO_STREAMING_REQUEST_ATTR, True)
    await ch.on_streaming_start(req, "user", None, {}, "message")

    # 命中标记即短路返回：连 receive 解析都不触发，更不会建卡片。
    assert recv_calls["recv"] == 0


async def test_on_streaming_start_normal_creates_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _make_channel()
    calls: Dict[str, int] = {"create": 0}
    state: Dict[str, Any] = {"cards": {}}

    async def fake_get_receive(*args: Any, **kwargs: Any) -> Any:
        return ("open_id", "ou_demo")

    def fake_stream_state(send_meta: Any) -> Dict[str, Any]:
        return state

    async def fake_create(*args: Any, **kwargs: Any) -> Dict[str, str]:
        calls["create"] += 1
        return {"card_id": "cv_demo", "message_id": "om_demo"}

    def fake_build_text(stream_type: str, text: str, send_meta: Any) -> str:
        return text

    monkeypatch.setattr(ch, "_get_receive_for_send", fake_get_receive)
    monkeypatch.setattr(ch, "_get_feishu_stream_state", fake_stream_state)
    monkeypatch.setattr(ch, "_create_streaming_card", fake_create)
    monkeypatch.setattr(ch, "_build_stream_display_text", fake_build_text)

    req = _make_request("正常对话")
    await ch.on_streaming_start(req, "user", None, {}, "message")

    assert calls["create"] == 1
    assert state["cards"]["message"]["card_id"] == "cv_demo"
