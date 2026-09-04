# -*- coding: utf-8 -*-
"""FeishuPlusChannel 触发规则管理命令测试（不触网）。

覆盖 ``/feishu-plus`` slash 命令背后的能力：

* ``describe_triggers`` —— show-triggers 的文本生成：有规则 / 无规则 /
  加载失败各状态下的展示。
* ``reload_triggers`` —— reload-triggers：改文件后免重启生效；失败时
  保留上一份生效规则并返回原因（快照回滚）。
* ``feishu_plus_command_handler`` —— 从 ctx 定位 feishu_plus 渠道实例并
  按子命令路由；渠道未启用 / 无参数 / 未知子命令的兜底回复。

handler 依赖的 channel_manager 以 fake 提供，不触真实 QwenPaw 服务。
"""
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import pytest

from qwenpaw_feishu_plus.channel import (
    FeishuPlusChannel,
    _CompiledTrigger,
    _TRIGGER_COMMAND_USAGE,
    feishu_plus_command_handler,
)

_TRIGGER_PATH_NAME = "feishu_plus_triggers.yaml"


def _rule(
    pattern: str,
    context: str = "",
    chat_ids: Tuple[str, ...] = (),
) -> _CompiledTrigger:
    """构造一条编译好的触发规则。"""
    return _CompiledTrigger(
        pattern=re.compile(pattern),
        context=context,
        chat_ids=chat_ids,
    )


def _make_channel(
    path: str = "",
    rules: Optional[List[_CompiledTrigger]] = None,
    error: str = "",
    skipped: int = 0,
    auto_thread: bool = False,
) -> FeishuPlusChannel:
    """构造跳过 __init__ 的实例并注入触发规则状态。"""
    ch = FeishuPlusChannel.__new__(FeishuPlusChannel)
    ch._trigger_yaml_path = path
    ch._trigger_rules = list(rules or [])
    ch._trigger_load_error = error
    ch._trigger_skipped = skipped
    ch._auto_thread_on_trigger = auto_thread
    return ch


def _msg_text(msg: Any) -> str:
    """取 slash 命令 handler 返回 Msg 的纯文本。"""
    assert msg is not None
    return msg.get_text_content() or ""


# ====================================================================
# describe_triggers（show-triggers）
# ====================================================================


def test_describe_triggers_lists_rules(tmp_path: Path) -> None:
    ch = _make_channel(
        path=str(tmp_path / _TRIGGER_PATH_NAME),
        rules=[
            _rule("^告警", "（运维告警场景，请按值班口径回复）", ("oc_a", "oc_b")),
            _rule("小助手", "", ()),
        ],
    )
    text = ch.describe_triggers()

    assert "生效 2 条" in text
    assert str(tmp_path / _TRIGGER_PATH_NAME) in text
    assert "auto_thread_on_trigger: 关" in text
    # 规则 1：context 与群白名单展示。
    assert "^告警" in text
    assert "运维告警场景" in text
    assert "群白名单: oc_a、oc_b" in text
    # 规则 2：纯触发、不限群。
    assert "小助手" in text
    assert "全部群" in text


def test_describe_triggers_reports_auto_thread(tmp_path: Path) -> None:
    ch = _make_channel(
        path=str(tmp_path / _TRIGGER_PATH_NAME),
        rules=[_rule("^告警", "ctx")],
        auto_thread=True,
    )
    assert "auto_thread_on_trigger: 开" in ch.describe_triggers()


def test_describe_triggers_no_file(tmp_path: Path) -> None:
    ch = _make_channel(path=str(tmp_path / _TRIGGER_PATH_NAME), rules=[])
    text = ch.describe_triggers()

    assert "生效 0 条" in text
    assert "文件不存在，未配置任何触发规则" in text


def test_describe_triggers_load_error(tmp_path: Path) -> None:
    ch = _make_channel(
        path=str(tmp_path / _TRIGGER_PATH_NAME),
        rules=[],
        error="文件解析失败（非法 YAML）",
    )
    text = ch.describe_triggers()

    assert "生效 0 条" in text
    assert "最近一次加载失败: 文件解析失败（非法 YAML）" in text


def test_describe_triggers_skipped_note(tmp_path: Path) -> None:
    ch = _make_channel(
        path=str(tmp_path / _TRIGGER_PATH_NAME),
        rules=[_rule("^告警", "ctx")],
        skipped=1,
    )
    assert "本次加载 1 条非法正则被跳过" in ch.describe_triggers()


# ====================================================================
# reload_triggers（reload-triggers）
# ====================================================================


def test_reload_triggers_picks_up_file_edit(tmp_path: Path) -> None:
    yaml_file = tmp_path / _TRIGGER_PATH_NAME
    yaml_file.write_text(
        'triggers:\n  - pattern: "^告警"\n',
        encoding="utf-8",
    )
    ch = _make_channel(path=str(yaml_file))
    ch._load_trigger_yaml(yaml_file)
    assert len(ch._trigger_rules) == 1

    # 修改文件后 reload：新规则免重启生效。
    yaml_file.write_text(
        'triggers:\n  - pattern: "^告警"\n  - pattern: "P[012]"\n',
        encoding="utf-8",
    )
    text = ch.reload_triggers()

    assert "触发规则已重新加载" in text
    assert "生效 2 条" in text
    assert len(ch._trigger_rules) == 2


def test_reload_triggers_failure_keeps_previous_rules(
    tmp_path: Path,
) -> None:
    # 上一份规则在运行中；文件被改成非法 YAML，reload 应回滚而非清空。
    yaml_file = tmp_path / _TRIGGER_PATH_NAME
    ch = _make_channel(
        path=str(yaml_file),
        rules=[_rule("^告警", "（正在生效的口径）")],
    )
    yaml_file.write_text(":::: not yaml [", encoding="utf-8")

    text = ch.reload_triggers()

    assert "重新加载失败" in text
    assert "已回滚" in text
    # 上一份生效规则保留，未被错误文件清空。
    assert len(ch._trigger_rules) == 1
    assert ch._trigger_rules[0][1] == "（正在生效的口径）"


def test_reload_triggers_missing_file_keeps_previous(tmp_path: Path) -> None:
    ch = _make_channel(
        path=str(tmp_path / _TRIGGER_PATH_NAME),
        rules=[_rule("^告警", "ctx")],
    )
    text = ch.reload_triggers()

    assert "重新加载失败" in text
    assert "文件不存在" in text
    assert len(ch._trigger_rules) == 1


def test_reload_triggers_reports_skipped(tmp_path: Path) -> None:
    yaml_file = tmp_path / _TRIGGER_PATH_NAME
    yaml_file.write_text(
        'triggers:\n  - pattern: "^告警"\n  - pattern: "([unclosed"\n',
        encoding="utf-8",
    )
    ch = _make_channel(path=str(yaml_file))
    ch._load_trigger_yaml(yaml_file)
    assert len(ch._trigger_rules) == 1

    text = ch.reload_triggers()

    # 文件整体合法，坏正则条目被跳过并明确提示。
    assert "生效 1 条" in text
    assert "1 条正则非法被跳过" in text


def test_reload_triggers_no_path() -> None:
    ch = _make_channel(path="")
    text = ch.reload_triggers()

    assert "未配置规则文件" in text
    assert ch._trigger_rules == []


# ====================================================================
# feishu_plus_command_handler（/feishu-plus 命令）
# ====================================================================


class _FakeChannelManager:
    """返回固定渠道实例的 channel_manager 替身。"""

    def __init__(self, channel: Any) -> None:
        self._channel = channel

    async def get_channel(self, _channel_id: str) -> Any:
        return self._channel


def _ctx_for(channel: Any) -> Any:
    """构造带 channel_manager 的最小 ctx。"""
    workspace = SimpleNamespace(
        channel_manager=_FakeChannelManager(channel),
    )
    return SimpleNamespace(workspace=workspace)


async def test_handler_show_triggers_routes() -> None:
    ch = _make_channel(
        path="/w/" + _TRIGGER_PATH_NAME,
        rules=[_rule("^告警", "值班口径")],
    )
    msg = await feishu_plus_command_handler(_ctx_for(ch), "show-triggers")

    text = _msg_text(msg)
    assert "生效 1 条" in text
    assert "^告警" in text
    assert "值班口径" in text


async def test_handler_reload_triggers_routes(tmp_path: Path) -> None:
    yaml_file = tmp_path / _TRIGGER_PATH_NAME
    yaml_file.write_text(
        'triggers:\n  - pattern: "^告警"\n',
        encoding="utf-8",
    )
    ch = _make_channel(path=str(yaml_file))
    msg = await feishu_plus_command_handler(_ctx_for(ch), "reload-triggers")

    text = _msg_text(msg)
    assert "触发规则已重新加载" in text
    assert "生效 1 条" in text
    assert len(ch._trigger_rules) == 1


async def test_handler_no_channel_returns_hint() -> None:
    msg = await feishu_plus_command_handler(_ctx_for(None), "show-triggers")

    assert "渠道未启用" in _msg_text(msg)


async def test_handler_no_args_returns_usage() -> None:
    ch = _make_channel(path="/w/" + _TRIGGER_PATH_NAME, rules=[])
    msg = await feishu_plus_command_handler(_ctx_for(ch), "")

    assert _msg_text(msg) == _TRIGGER_COMMAND_USAGE


async def test_handler_unknown_subcommand_returns_usage() -> None:
    ch = _make_channel(path="/w/" + _TRIGGER_PATH_NAME, rules=[])
    msg = await feishu_plus_command_handler(_ctx_for(ch), "frobnicate")

    text = _msg_text(msg)
    assert "未知子命令: frobnicate" in text
    assert _TRIGGER_COMMAND_USAGE in text


async def test_handler_workspace_without_manager() -> None:
    # workspace 无 channel_manager（极端情形）不抛异常，按未启用兜底。
    ctx = SimpleNamespace(workspace=SimpleNamespace())
    msg = await feishu_plus_command_handler(ctx, "show-triggers")

    assert "渠道未启用" in _msg_text(msg)
