# -*- coding: utf-8 -*-
"""话题感知的 tool-guard 审批卡片 render + handle。

完全复用 ``qwenpaw.app.channels.feishu.cards.tool_guard`` 的**无状态**
构造/解析函数（``build_approval_card`` / ``build_resolved_card`` /
``parse_action_value`` / ``build_toast``），以及 ``context`` 的会话上下文
构造，但把两处「话题路由」逻辑收敛到本模块：

* ``render`` —— 话题内用 ``_reply_in_thread`` + ``interactive`` 发卡片，
  并把 ``feishu_thread_id`` / ``feishu_message_id`` 强制写入 button 的
  ``session_ctx``（不依赖 ``context.build_session_ctx`` 是否含这两个字段）。
* ``handle`` —— 按钮点击时从 ``session_ctx`` 读回 thread，让 ``/approval``
  命令落回原话题（不依赖 ``tool_guard._enqueue_approval_command`` 是否含
  thread 恢复逻辑）。

这样可把 site-packages 的 ``tool_guard.py`` / ``context.py`` 恢复成上游
版本，插件仍完整具备话题能力。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

from qwenpaw.app.channels.feishu.cards import context, tool_guard

try:
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )
except ImportError:  # pragma: no cover - optional dependency
    P2CardActionTriggerResponse = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ====================================================================
# Outbound: render —— 话题感知发送
# ====================================================================


async def render_tool_guard_enhanced(
    channel: Any,
    to_handle: str,
    event: Any,
    send_meta: Dict[str, Any],
    meta: Dict[str, Any],
    **_kwargs: Any,
) -> bool:
    """发送一张工具审批 interactive 卡片（话题感知）。"""
    if not meta.get("approval_request_id"):
        return False
    if not channel.enabled:
        return False

    recv = await channel._get_receive_for_send(to_handle, send_meta)
    if not recv:
        logger.warning(
            "feishu-plus approval card: no receive_id for to_handle=%s",
            (to_handle or "")[:50],
        )
        return False

    receive_id_type, receive_id = recv
    body_text = context.extract_body_text(getattr(event, "content", None))
    session_ctx = context.build_session_ctx(
        to_handle,
        send_meta,
        receive_id,
        receive_id_type,
    )
    # 强制写入话题路由字段（直接赋值，不依赖 build_session_ctx 的版本）。
    # 这些字段会被 build_approval_card 存进 button value，供 handle 恢复。
    session_ctx["feishu_thread_id"] = str(
        send_meta.get("feishu_thread_id") or "",
    )
    session_ctx["feishu_message_id"] = str(
        send_meta.get("feishu_message_id") or "",
    )

    content = tool_guard.build_approval_card(
        request_id=str(meta.get("approval_request_id") or ""),
        tool_name=str(meta.get("tool_name") or "tool"),
        severity=str(meta.get("severity") or "medium"),
        body_text=body_text,
        session_ctx=session_ctx,
    )

    # 话题内回复到 thread；否则发到会话根。两路都用 interactive 卡片。
    thread_msg_id = ""
    if send_meta and send_meta.get("feishu_thread_id"):
        thread_msg_id = send_meta.get("feishu_message_id", "")
    if thread_msg_id:
        msg_id = await channel._reply_in_thread(
            thread_msg_id,
            "interactive",
            content,
        )
    else:
        msg_id = await channel._send_message(
            receive_id_type,
            receive_id,
            "interactive",
            content,
        )

    if msg_id:
        send_meta["_last_sent_message_id"] = msg_id
        logger.info(
            "feishu-plus approval card sent: request_id=%s msg_id=%s "
            "thread=%s",
            str(meta.get("approval_request_id") or "")[:8],
            msg_id[:24],
            bool(thread_msg_id),
        )
        return True
    logger.warning(
        "feishu-plus approval card send failed: request_id=%s",
        str(meta.get("approval_request_id") or "")[:8],
    )
    return False


# ====================================================================
# Inbound: handle —— 话题感知 /approval 回注
# ====================================================================


def handle_tool_guard_enhanced(
    channel: Any,
    event: Any,
    action_value: Dict[str, Any],
) -> Any:
    """处理审批按钮点击（同步），把 /approval 命令回注到原话题。"""
    parsed = tool_guard.parse_action_value(action_value)
    if not parsed:
        return P2CardActionTriggerResponse({})

    action = parsed["action"]
    operator = getattr(event, "operator", None) if event else None
    operator_open_id = (
        getattr(operator, "open_id", None) if operator else None
    ) or ""

    # 话题感知回注：从 session_ctx 恢复 thread 路由。
    _enqueue_approval_command_threaded(
        channel,
        action=action,
        request_id=parsed["request_id"],
        session_ctx=parsed.get("session_ctx") or {},
        operator_open_id=operator_open_id,
    )

    tool_name = parsed.get("tool_name") or "tool"

    # 解析操作者显示名（在主 loop 上调度协程）。
    operator_display = operator_open_id[-6:] if operator_open_id else ""
    loop = channel._loop
    if operator_open_id and loop and loop.is_running():
        try:
            name = asyncio.run_coroutine_threadsafe(
                channel._get_user_name_by_open_id(operator_open_id),
                loop,
            ).result(timeout=2)
            if name:
                operator_display = name
        except Exception:
            pass

    resolved_card = tool_guard.build_resolved_card(
        tool_name=tool_name,
        action=action,
        operator_display=operator_display,
        body_text=parsed.get("body") or "",
    )
    toast = tool_guard.build_toast(action, tool_name)
    try:
        return P2CardActionTriggerResponse(
            {
                "toast": toast,
                "card": {
                    "type": "raw",
                    "data": json.loads(resolved_card),
                },
            },
        )
    except Exception:  # pragma: no cover
        logger.exception("feishu-plus card action: build response failed")
        return P2CardActionTriggerResponse({"toast": toast})


def _enqueue_approval_command_threaded(
    channel: Any,
    *,
    action: str,
    request_id: str,
    session_ctx: Dict[str, Any],
    operator_open_id: str,
) -> None:
    """把 ``/approval {action} {request_id}`` 注入渠道队列（话题感知）。

    从 ``session_ctx`` 读回卡片构建时捕获的 thread 路由，让回注命令
    落在原话题内。本函数是 ``tool_guard._enqueue_approval_command`` 的
    话题版副本，使其不依赖 site-packages 版本。
    """
    from qwenpaw.schemas import ContentType, TextContent

    enqueue = getattr(channel, "_enqueue", None)
    if enqueue is None:
        logger.warning(
            "feishu-plus card action: channel enqueue not set, drop "
            "%s %s",
            action,
            request_id[:8],
        )
        return

    sender_id = str(
        session_ctx.get("sender_id") or operator_open_id or "",
    )
    session_id = str(session_ctx.get("session_id") or "")
    receive_id = str(session_ctx.get("receive_id") or "")
    receive_id_type = str(
        session_ctx.get("receive_id_type") or "open_id",
    )
    chat_id = str(session_ctx.get("chat_id") or "")
    chat_type = str(session_ctx.get("chat_type") or "p2p")
    is_group = bool(session_ctx.get("is_group"))

    command_text = f"/approval {action} {request_id}".strip()
    content_parts = [
        TextContent(type=ContentType.TEXT, text=command_text),
    ]
    # 话题路由恢复：让 /approval 命令回到原话题。
    thread_id = str(session_ctx.get("feishu_thread_id") or "")
    thread_msg_id = str(session_ctx.get("feishu_message_id") or "")
    meta: Dict[str, Any] = {
        "feishu_sender_id": sender_id,
        "feishu_chat_id": chat_id,
        "feishu_chat_type": chat_type,
        "feishu_receive_id": receive_id,
        "feishu_receive_id_type": receive_id_type,
        "is_group": is_group,
        "from_card_action": True,
    }
    if thread_id:
        meta["feishu_thread_id"] = thread_id
        meta["feishu_message_id"] = thread_msg_id
    payload = {
        "channel_id": channel.channel,
        "sender_id": sender_id,
        "user_id": sender_id,
        "session_id": session_id,
        "content_parts": content_parts,
        "meta": meta,
    }
    try:
        enqueue(payload)
        logger.info(
            "feishu-plus card action enqueued: cmd=%s request=%s "
            "session=%s thread=%s",
            command_text,
            request_id[:8],
            session_id[:12],
            bool(thread_id),
        )
    except Exception:  # pragma: no cover
        logger.exception(
            "feishu-plus card action: enqueue command failed %s %s",
            action,
            request_id[:8],
        )
