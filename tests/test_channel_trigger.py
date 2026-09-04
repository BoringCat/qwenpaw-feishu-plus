# -*- coding: utf-8 -*-
"""FeishuPlusChannel 正则触发规则测试（不触网）。

用 ``__new__`` 绕过 ``__init__``（其需要 process / app 配置），
``Trigger`` 实例承载生效规则与话题开关；``_on_message`` wrapper 的
父类调用以 stub 替换，只验证 wrapper 自身的注入 / 追加 / ContextVar
行为。
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest
from lark_oapi.api.im.v1.model.event_message import EventMessage

from qwenpaw_feishu_plus.channel import (
    FeishuPlusChannel,
    _TRIGGER_MATCHED,
    Trigger,
)
from qwenpaw_feishu_plus.models import (
    CompiledTrigger
)

_MSG_ID = "om_aaa0123456789abcdef0123456789bbb"
_CHAT_ID = "oc_demo"


def _make_channel(
    rules: List[Tuple[Any, ...]],
    auto_thread: bool = False,
    require_mention: bool = True,
) -> FeishuPlusChannel:
    """构造跳过 __init__ 的实例并注入触发规则。

    ``rules`` 元素支持 (pattern, context) 或 (pattern, context, chat_ids)
    两种形态；chat_ids 缺省为空（不限群）。
    """
    ch = FeishuPlusChannel.__new__(FeishuPlusChannel)
    ch._trigger = Trigger([
        CompiledTrigger(
            pattern=re.compile(item[0]),
            context=item[1] if len(item) > 1 else "",
            chat_ids=tuple(item[2]) if len(item) > 2 else (),
        )
        for item in rules
    ])
    ch._trigger.auto_thread = auto_thread
    ch.require_mention = require_mention
    return ch


def _text_message(
    text: str,
    *,
    chat_type: str = "group",
    message_type: str = "text",
    thread_id: str = "",
    message_id: str = _MSG_ID,
    chat_id: str = _CHAT_ID,
) -> EventMessage:
    """构造 group text 事件消息（content 为飞书 text 消息 JSON）。"""
    return EventMessage(
        d={
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "message_type": message_type,
            "thread_id": thread_id,
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    )


# ====================================================================
# Trigger.load —— YAML 加载
#
# load 只做「文件 → 生效规则」的单向替换：文件缺失 / 非法 YAML /
# 结构错误均视为加载失败 —— 失败时**保留上一份生效规则**（reload
# 回滚的根基），原因记入 Trigger 供 describe_triggers 反馈；仅整体
# 合法的文件才会替换规则，其中正则非法的单条被跳过。任何分支都
# 不抛异常。
# ====================================================================


def _load_yaml(ch: FeishuPlusChannel, yaml_file: Path, body: str) -> None:
    """把 body 写入 yaml_file，并让渠道的 Trigger 从该文件加载。"""
    yaml_file.write_text(body, encoding="utf-8")
    ch._trigger.config_file = str(yaml_file)
    ch._trigger.load()


def _assert_failed_load_keeps_previous(
    ch: FeishuPlusChannel,
    reason: str,
) -> None:
    """断言加载失败后：上一份生效规则保留，失败原因经 describe 暴露。"""
    rules = ch._trigger.rules
    assert len(rules) == 1
    assert rules[0].pattern.pattern == "old"
    assert rules[0].context == "ctx"
    assert reason in ch.describe_triggers()


def test_load_trigger_yaml_ok(tmp_path: Path) -> None:
    ch = _make_channel([])
    _load_yaml(
        ch,
        tmp_path / "triggers.yaml",
        "triggers:\n"
        '  - pattern: "^告警"\n'
        '    context: "按值班口径回复"\n'
        '  - pattern: "小助手"\n',
    )

    rules = ch._trigger.rules
    assert len(rules) == 2
    assert rules[0].pattern.pattern == "^告警"
    assert rules[0].context == "按值班口径回复"
    # context 可选：缺省为空串（纯触发规则）。
    assert rules[1].pattern.pattern == "小助手"
    assert rules[1].context == ""


def test_load_trigger_yaml_invalid_regex_skipped(tmp_path: Path) -> None:
    ch = _make_channel([])
    _load_yaml(
        ch,
        tmp_path / "triggers.yaml",
        "triggers:\n"
        '  - pattern: "([unclosed"\n'
        '  - pattern: "P[012]"\n',
    )

    # 坏正则条目跳过，其余正常生效（文件整体合法，替换规则）。
    rules = ch._trigger.rules
    assert len(rules) == 1
    assert rules[0].pattern.pattern == "P[012]"
    # 跳过条数经 describe 对运维可见。
    assert "本次加载 1 条非法正则被跳过" in ch.describe_triggers()


def test_load_trigger_yaml_extra_field_rejected(tmp_path: Path) -> None:
    ch = _make_channel([("old", "ctx")])
    _load_yaml(
        ch,
        tmp_path / "triggers.yaml",
        "triggers:\n"
        '  - pattern: "^告警"\n'
        '    contextx: "拼写错误字段"\n',  # 多余字段 → 结构错误
    )
    _assert_failed_load_keeps_previous(ch, "文件结构非法")


def test_load_trigger_yaml_non_string_pattern_rejected(tmp_path: Path) -> None:
    ch = _make_channel([("old", "ctx")])
    _load_yaml(
        ch,
        tmp_path / "triggers.yaml",
        "triggers:\n"
        "  - pattern: 42\n",  # pattern 非字符串 → 结构错误
    )
    _assert_failed_load_keeps_previous(ch, "文件结构非法")


@pytest.mark.parametrize("raw", ['""', '"   "'])
def test_load_trigger_yaml_empty_pattern_rejected(
    tmp_path: Path,
    raw: str,
) -> None:
    ch = _make_channel([("old", "ctx")])
    _load_yaml(
        ch,
        tmp_path / "triggers.yaml",
        "triggers:\n"
        f"  - pattern: {raw}\n",  # 空 / 纯空白 pattern → 结构错误
    )
    _assert_failed_load_keeps_previous(ch, "文件结构非法")


def test_load_trigger_yaml_strips_and_null_context(tmp_path: Path) -> None:
    ch = _make_channel([])
    _load_yaml(
        ch,
        tmp_path / "triggers.yaml",
        "triggers:\n"
        '  - pattern: "  告警  "\n'
        '    context: "  按值班口径  "\n'
        "  - pattern: P[012]\n"
        "    context:\n",  # 显式 null → 视作空串
    )

    rules = ch._trigger.rules
    assert len(rules) == 2
    assert rules[0].pattern.pattern == "告警"
    assert rules[0].context == "按值班口径"
    assert rules[1].pattern.pattern == "P[012]"
    assert rules[1].context == ""


@pytest.mark.parametrize(
    "content,reason",
    [
        pytest.param(
            "triggers: 42", "文件结构非法", id="triggers-not-list",
        ),
        pytest.param(
            "just_a_string", "文件结构非法", id="top-level-not-mapping",
        ),
        pytest.param(
            "a: [unclosed", "文件解析失败", id="bad-yaml",
        ),
    ],
)
def test_load_trigger_yaml_bad_shape_keeps_previous(
    tmp_path: Path,
    content: str,
    reason: str,
) -> None:
    # 非法 YAML / 顶层结构错误 → 加载失败：保留旧规则，不抛异常。
    ch = _make_channel([("old", "ctx")])
    _load_yaml(ch, tmp_path / "triggers.yaml", content)
    _assert_failed_load_keeps_previous(ch, reason)


def test_load_trigger_yaml_missing_file(tmp_path: Path) -> None:
    # 非默认文件名缺失 → 失败分支（默认名缺失视为未配置，走成功）。
    ch = _make_channel([("old", "ctx")])
    ch._trigger.config_file = str(tmp_path / "absent.yaml")
    ch._trigger.load()

    _assert_failed_load_keeps_previous(ch, "文件不存在")


def test_load_trigger_yaml_chat_ids(tmp_path: Path) -> None:
    ch = _make_channel([])
    _load_yaml(
        ch,
        tmp_path / "triggers.yaml",
        "triggers:\n"
        '  - pattern: "^告警"\n'
        '    context: "值班"\n'
        "    chat_ids:\n"
        '      - " oc_a "\n'
        "      - oc_b\n"
        '  - pattern: "小助手"\n',  # 缺省 chat_ids → 不限群
    )

    rules = ch._trigger.rules
    assert len(rules) == 2
    # chat_ids 条目去首尾空白。
    assert rules[0].chat_ids == ("oc_a", "oc_b")
    # 缺省为空元组（不限群）。
    assert rules[1].chat_ids == ()


def test_load_trigger_yaml_chat_ids_null_is_empty(tmp_path: Path) -> None:
    ch = _make_channel([])
    _load_yaml(
        ch,
        tmp_path / "triggers.yaml",
        "triggers:\n"
        '  - pattern: "^告警"\n'
        "    chat_ids:\n",  # 显式 null → 不限群
    )

    assert ch._trigger.rules[0].chat_ids == ()


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param("42", id="non-string"),
        pytest.param('""', id="empty-string"),
    ],
)
def test_load_trigger_yaml_bad_chat_ids_rejected(
    tmp_path: Path,
    entry: str,
) -> None:
    ch = _make_channel([("old", "ctx")])
    _load_yaml(
        ch,
        tmp_path / "triggers.yaml",
        "triggers:\n"
        '  - pattern: "^告警"\n'
        "    chat_ids:\n"
        f"      - {entry}\n",  # 非字符串 / 空条目 → 结构错误
    )
    _assert_failed_load_keeps_previous(ch, "文件结构非法")


def test_load_trigger_yaml_chat_ids_scalar_rejected(tmp_path: Path) -> None:
    ch = _make_channel([("old", "ctx")])
    _load_yaml(
        ch,
        tmp_path / "triggers.yaml",
        "triggers:\n"
        '  - pattern: "^告警"\n'
        '    chat_ids: "oc_a"\n',  # 标量而非列表 → 结构错误
    )
    _assert_failed_load_keeps_previous(ch, "文件结构非法")


# ====================================================================
# _trigger.match
# ====================================================================


def test_trigger_match_literal_and_first_hit_context() -> None:
    ch = _make_channel(
        [
            (r"^小助手", "场景A"),
            (r"告警", "场景B"),
            (r"P[012]", ""),
        ],
    )
    # 多规则命中：取第一条命中的 context。
    matched, ctx = ch._trigger.match(_text_message("P1 告警了"))
    assert matched is True
    assert ctx == "场景B"

    matched, ctx = ch._trigger.match(_text_message("无关内容"))
    assert matched is False
    assert ctx == ""


def test_trigger_match_anchor_and_case() -> None:
    ch = _make_channel([(r"^小助手", "ctx"), ((r"(?i)\bHELP\b"), "")])

    # ^ 锚定：不在行首不命中。
    assert ch._trigger.match(_text_message("帮我叫 小助手"))[0] is False
    assert ch._trigger.match(_text_message("小助手 帮我"))[0] is True
    # (?i) 大小写不敏感。
    assert ch._trigger.match(_text_message("help me"))[0] is True


def test_trigger_match_not_group_or_not_text() -> None:
    ch = _make_channel([(r"告警", "ctx")])

    # p2p 不参与触发（私聊本就必达）。
    assert ch._trigger.match(_text_message("告警", chat_type="p2p"))[
        0
    ] is False
    # 非 text 类型不匹配。
    assert ch._trigger.match(
        _text_message("告警", message_type="post"),
    )[0] is False


def test_trigger_match_empty_rules() -> None:
    ch = _make_channel([])
    assert ch._trigger.match(_text_message("告警")) == (False, "")


def test_trigger_match_bad_content_json() -> None:
    ch = _make_channel([(r"告警", "")])
    msg = _text_message("告警")
    msg.content = "not json"
    assert ch._trigger.match(msg) == (False, "")


def test_trigger_match_chat_ids_whitelist() -> None:
    # 规则1 限定 _CHAT_ID；规则2 不限群 —— 取第一条可命中的规则。
    ch = _make_channel(
        [
            (r"告警", "场景A", (_CHAT_ID,)),
            (r"告警", "场景B"),
        ],
    )
    # 白名单内的群：规则1 命中。
    assert ch._trigger.match(_text_message("告警了")) == (True, "场景A")
    # 白名单之外的群：跳过规则1，规则2 命中。
    assert ch._trigger.match(
        _text_message("告警了", chat_id="oc_other"),
    ) == (True, "场景B")


def test_trigger_match_chat_ids_no_match_outside() -> None:
    ch = _make_channel([(r"告警", "ctx", (_CHAT_ID,))])
    assert ch._trigger.match(_text_message("告警")) == (True, "ctx")
    assert ch._trigger.match(
        _text_message("告警", chat_id="oc_other"),
    ) == (False, "")


def test_trigger_match_chat_ids_multi_group() -> None:
    ch = _make_channel([(r"告警", "ctx", ("oc_a", "oc_b"))])
    assert ch._trigger.match(_text_message("告警", chat_id="oc_b")) == (
        True,
        "ctx",
    )
    assert ch._trigger.match(
        _text_message("告警", chat_id="oc_c"),
    ) == (False, "")


def test_trigger_match_chat_ids_blank_message_chat() -> None:
    # 规则限定群但消息缺 chat_id：不命中；不限群的规则仍命中。
    ch = _make_channel(
        [
            (r"告警", "ctxA", ("oc_a",)),
            (r"告警", "ctxB"),
        ],
    )
    assert ch._trigger.match(
        _text_message("告警", chat_id=""),
    ) == (True, "ctxB")


# ====================================================================
# _check_group_mention
# ====================================================================


def test_check_group_mention_trigger_bypass() -> None:
    ch = _make_channel([], require_mention=True)

    # 未命中且无 mention：透传父类 → False。
    assert ch._check_group_mention(True, {}) is False

    # 命中（ContextVar）：绕过 @提及 检查。
    token = _TRIGGER_MATCHED.set(True)
    try:
        assert ch._check_group_mention(True, {}) is True
    finally:
        _TRIGGER_MATCHED.reset(token)

    # 复位后恢复父类行为。
    assert ch._check_group_mention(True, {}) is False


def test_check_group_mention_p2p_passthrough() -> None:
    ch = _make_channel([], require_mention=True)
    # p2p 恒为 True（父类行为）。
    assert ch._check_group_mention(False, {}) is True


# ====================================================================
# _on_message wrapper
# ====================================================================


def _stub_super(monkeypatch: pytest.MonkeyPatch) -> dict:
    """把父类 _on_message 换成记录型 stub，返回调用记录。"""
    from qwenpaw.app.channels.feishu.channel import FeishuChannel

    record: dict = {"calls": 0, "flag": None, "data": None}

    async def fake_on_message(self: Any, data: Any) -> None:
        record["calls"] += 1
        record["flag"] = _TRIGGER_MATCHED.get()
        record["data"] = data

    monkeypatch.setattr(FeishuChannel, "_on_message", fake_on_message)
    return record


def test_on_message_auto_thread_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _stub_super(monkeypatch)
    ch = _make_channel([("告警", "")], auto_thread=True)
    msg = _text_message("P0 告警")

    import asyncio

    asyncio.run(ch._on_message(SimpleNamespace(event=SimpleNamespace(message=msg))))

    assert record["calls"] == 1
    # 注入 thread_id = message_id：父类话题管道随之生效。
    assert msg.thread_id == _MSG_ID
    # ContextVar 在父类调用内可见，调用后复位。
    assert record["flag"] is True
    assert _TRIGGER_MATCHED.get() is False
    # 纯触发规则：content 不改写。
    assert json.loads(msg.content) == {"text": "P0 告警"}


def test_on_message_existing_thread_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _stub_super(monkeypatch)
    ch = _make_channel([("告警", "")], auto_thread=True)
    msg = _text_message("告警", thread_id="om_existing_thread")

    import asyncio

    asyncio.run(ch._on_message(SimpleNamespace(event=SimpleNamespace(message=msg))))

    assert msg.thread_id == "om_existing_thread"


def test_on_message_auto_thread_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _stub_super(monkeypatch)
    ch = _make_channel([("告警", "")], auto_thread=False)
    msg = _text_message("告警")

    import asyncio

    asyncio.run(ch._on_message(SimpleNamespace(event=SimpleNamespace(message=msg))))

    assert msg.thread_id == ""
    assert record["calls"] == 1


def test_on_message_context_appended_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_super(monkeypatch)
    ch = _make_channel([("告警", "（运维告警场景，请按值班口径回复）")])
    msg = _text_message("P1 告警")

    import asyncio

    asyncio.run(ch._on_message(SimpleNamespace(event=SimpleNamespace(message=msg))))

    # context 作为一行追加到 text 末尾，仍是合法的飞书 text content。
    payload = json.loads(msg.content)
    assert payload["text"] == "P1 告警\n（运维告警场景，请按值班口径回复）"


def test_on_message_no_match_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _stub_super(monkeypatch)
    ch = _make_channel([("告警", "ctx")], auto_thread=True)
    msg = _text_message("今天天气不错")

    import asyncio

    asyncio.run(ch._on_message(SimpleNamespace(event=SimpleNamespace(message=msg))))

    assert msg.thread_id == ""
    assert json.loads(msg.content) == {"text": "今天天气不错"}
    # 未命中不置 ContextVar。
    assert record["flag"] is False


def test_on_message_chat_ids_gate_prevents_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 规则限定 _CHAT_ID；消息来自其他群 —— 正文虽命中但整体不触发：
    # 不注入 thread_id、不改写 content、ContextVar 保持 False。
    record = _stub_super(monkeypatch)
    ch = _make_channel([("告警", "ctx", (_CHAT_ID,))], auto_thread=True)
    msg = _text_message("告警", chat_id="oc_other")

    import asyncio

    asyncio.run(ch._on_message(SimpleNamespace(event=SimpleNamespace(message=msg))))

    assert record["calls"] == 1
    assert msg.thread_id == ""
    assert json.loads(msg.content) == {"text": "告警"}
    assert record["flag"] is False
