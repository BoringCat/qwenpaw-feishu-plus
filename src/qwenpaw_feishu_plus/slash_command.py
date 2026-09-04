# -*- coding: utf-8 -*-
'''
/feishu-plus 管理命令（slash command）handler

``plugin.py`` 经 ``api.register_slash_command`` 注册到每个 workspace 的
``SlashCommandRegistry``；用户文本 ``/feishu-plus <sub>`` 在
``Runtime.run`` 的固定命令阶段被 dispatch，剩余文本即 ``args``。handler
从 ctx 的 workspace channel_manager 定位当前 ``feishu_plus`` 渠道实例，
调用其触发规则支持方法并把结果作为回复 Msg 返回（协议与内置 control
命令一致，见 runtime/builtin_commands.py）。
'''

from .channel import FeishuPlusChannel
from types import SimpleNamespace
from agentscope.message import Msg, TextBlock
from qwenpaw.runtime.hooks import HookContext
from qwenpaw.app.workspace.workspace import Workspace
from qwenpaw.app.channels.manager import ChannelManager

# ``/feishu-plus`` 命令（show-triggers / reload-triggers）的用法说明，
# 无参数或未知子命令时展示给用户。
_TRIGGER_COMMAND_USAGE = (
    "feishu-plus 触发规则管理命令\n\n"
    "用法:\n"
    "  /feishu-plus show-triggers      查看当前生效的触发规则配置\n"
    "  /feishu-plus reload-triggers    重新加载规则 YAML 文件"
)

def _command_reply(text: str) -> Msg:
    """把命令结果文本包装成 slash 命令回复的 agentscope ``Msg``。"""

    return Msg(
        name="assistant",
        role="assistant",
        content=[TextBlock(type="text", text=text)],
    )


async def feishu_plus_command_handler(ctx: HookContext, args: str) -> Msg|None:
    """``/feishu-plus`` 触发规则管理命令 handler。

    子命令：``show-triggers``（查看当前触发配置）、``reload-triggers``
    （重新加载规则 YAML）。无参数或未知子命令时返回用法说明。
    """
    # 定位当前 workspace 的 feishu_plus 渠道实例（启用时才存在）。
    workspace:Workspace = getattr(ctx, "workspace", None)
    channel = None
    manager:ChannelManager = (
        getattr(workspace, "channel_manager", None)
        if workspace is not None
        else None
    )
    if manager is not None:
        try:
            candidate = await manager.get_channel("feishu_plus")
        except Exception:
            candidate = None
        if isinstance(candidate, FeishuPlusChannel):
            channel = candidate
    if channel is None:
        return _command_reply(
            "「飞书+」渠道未启用，无法执行该命令。\n\n" + _TRIGGER_COMMAND_USAGE,
        )

    sub = (args or "").strip().lower()
    if not sub or sub == "help":
        return _command_reply(_TRIGGER_COMMAND_USAGE)
    if sub == "show-triggers":
        return _command_reply(channel.describe_triggers())
    if sub == "reload-triggers":
        return _command_reply(channel.reload_triggers())
    return _command_reply(f"未知子命令: {sub}\n\n" + _TRIGGER_COMMAND_USAGE)
