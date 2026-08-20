# -*- coding: utf-8 -*-
"""FeishuPlusChannel —— 继承内置 FeishuChannel 的增强飞书渠道。

channel key = ``feishu_plus``（独立 key，避免与内置 feishu 冲突而被
registry 跳过）。全部收发 / WebSocket / CardKit / 媒体能力继承自父类，
本类只做四件事：

1. 覆盖 tool-guard 审批卡片的 render —— 话题内走 ``_reply_in_thread``
   + ``msg_type="interactive"``（见 cards_override）。
2. 放开话题内流式输出的三处跳过（实验性，依赖飞书话题对 CardKit
   interactive 卡片的支持；失败自动回退纯文本）。
3. 以 ``/`` 开头的消息（控制命令）跳过引用消息获取 —— 用户回复
   机器人卡片时输入命令，父类抓回引用的 interactive 卡片内容并前置
   ``[quoted interactive: ...]``，会让命令文本不再以 ``/`` 开头。
4. interactive 卡片渲染为结构化 Markdown（见 card_markdown）——
   父类 ``extract_interactive_text`` 会把卡片压成单行，且 CardKit v2
   ``div`` 的正文（在 ``text.content`` 键）整体丢失；本类对直接收到的
   interactive 消息与被引用（quoted）卡片都改为完整 Markdown 渲染，
   quoted 时以 ``> `` 引用块前置。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from qwenpaw.app.channels.feishu.channel import FeishuChannel, _MSG_TYPE_LABEL
from qwenpaw.app.channels.feishu.constants import FEISHU_STREAM_ELEMENT_ID
from qwenpaw.app.channels.feishu.utils import short_session_id_from_full_id
from qwenpaw.app.channels.renderer import ChannelDisplayConfig

from .card_markdown import interactive_card_to_markdown, quote_lines

logger = logging.getLogger(__name__)


def _quote_block(text: str) -> str:
    """把 Markdown 文本转为 markdown 引用块（每行 ``> `` 前缀）。

    空行渲染为单独的 ``>`` 保持引用块连续；块尾补一个空行，使
    ``text_parts`` 以 ``\\n`` join 后引用块与用户正文之间有空行分隔。
    """
    return quote_lines(text) + "\n"


class FeishuPlusChannel(FeishuChannel):
    """话题感知的飞书渠道。"""

    channel = "feishu_plus"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 父类 __init__ 已创建 self._card_handler（FeishuCardHandler）。
        # 用 dispatcher 的 public register() 覆盖 tool_guard 的 CardKind，
        # 把 render + handle 都换成话题感知版本（见 cards_override），
        # 这样恢复 site-packages 的 tool_guard.py / context.py 上游版本后，
        # 插件仍完整具备话题能力。
        from qwenpaw.app.channels.feishu.cards import tool_guard
        from qwenpaw.app.channels.feishu.cards.dispatcher import CardKind

        from .cards_override import (
            handle_tool_guard_enhanced,
            render_tool_guard_enhanced,
        )

        self._card_handler.register(
            CardKind(
                name=tool_guard.NAME,
                message_type=tool_guard.MESSAGE_TYPE,
                action_type=tool_guard.ACTION_TYPE,
                render=render_tool_guard_enhanced,
                handle=handle_tool_guard_enhanced,
            ),
        )
        logger.info(
            "feishu-plus: tool_guard render+handle overridden (thread-aware)",
        )

    # ------------------------------------------------------------------
    # from_config —— 插件频道的 config 是 SimpleNamespace（非 Pydantic）
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        process: Any,
        config: Any,
        on_reply_sent: Any = None,
        display_config: Optional[ChannelDisplayConfig] = None,
        no_text_debounce: bool = True,
        workspace_dir: Optional[Path] = None,
    ) -> "FeishuPlusChannel":
        """从 SimpleNamespace 配置创建实例。

        插件频道的 ``config`` 是 ``types.SimpleNamespace``，必须用
        ``getattr`` 安全读取（详见官方插件文档「示例 10」）。
        """
        return cls(
            process=process,
            enabled=bool(getattr(config, "enabled", False)),
            app_id=getattr(config, "app_id", "") or "",
            app_secret=getattr(config, "app_secret", "") or "",
            bot_prefix=getattr(config, "bot_prefix", "") or "",
            encrypt_key=getattr(config, "encrypt_key", "") or "",
            verification_token=getattr(config, "verification_token", "") or "",
            media_dir=getattr(config, "media_dir", "") or "",
            workspace_dir=workspace_dir,
            on_reply_sent=on_reply_sent,
            display_config=(
                display_config or ChannelDisplayConfig.from_config(config)
            ),
            no_text_debounce=no_text_debounce,
            dm_policy=getattr(config, "dm_policy", "open") or "open",
            group_policy=getattr(config, "group_policy", "open") or "open",
            allow_from=getattr(config, "allow_from", None) or [],
            deny_message=getattr(config, "deny_message", "") or "",
            require_mention=bool(getattr(config, "require_mention", False)),
            domain=getattr(config, "domain", "feishu") or "feishu",
            streaming_enabled=bool(getattr(config, "streaming_enabled", False)),
            share_session_in_group=bool(
                getattr(config, "share_session_in_group", False),
            ),
            access_control_dm=bool(
                getattr(config, "access_control_dm", False),
            ),
            access_control_group=bool(
                getattr(config, "access_control_group", False),
            ),
        )

    # ------------------------------------------------------------------
    # resolve_session_id —— 话题感知的 session 聚合
    # ------------------------------------------------------------------

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """话题内的群消息按 thread_id 聚合 session（而非 chat_id），
        使同一话题共享一个会话上下文。

        该逻辑收敛自 ``FeishuChannel.resolve_session_id`` —— 把它放进
        插件可保证即便上游该方法发生变化，话题聚合行为仍稳定。
        ``_on_message`` 与 ``build_agent_request_from_native`` 都通过
        ``self.resolve_session_id`` 调用，会走到本覆写。
        """
        meta = channel_meta or {}
        chat_id = (meta.get("feishu_chat_id") or "").strip()
        thread_id = (meta.get("feishu_thread_id") or "").strip()
        chat_type = (meta.get("feishu_chat_type") or "p2p").strip()
        if chat_type == "group" and (chat_id or thread_id):
            # app_id 后缀区分同群多个 bot
            app_suffix = (
                self.app_id[-4:] if len(self.app_id) >= 4 else self.app_id
            )
            if thread_id:
                return f"{app_suffix}_{short_session_id_from_full_id(thread_id)}"
            return f"{app_suffix}_{short_session_id_from_full_id(chat_id)}"
        if sender_id:
            return short_session_id_from_full_id(sender_id)
        if chat_id:
            return short_session_id_from_full_id(chat_id)
        return f"{self.channel}:{sender_id}"

    # ------------------------------------------------------------------
    # _parse_message_content —— interactive 卡片 → 结构化 Markdown
    # ------------------------------------------------------------------

    async def _parse_message_content(  # type: ignore[override]
        self,
        msg_type: str,
        content_raw: str,
        message_id: str,
    ) -> Tuple[Optional[str], List[str], List[Any]]:
        """interactive 消息渲染为结构化 Markdown（其余类型透传父类）。

        父类对 interactive 调 ``extract_interactive_text``：把卡片压成
        单行，且 CardKit v2 ``div`` 正文在 ``text.content`` 键 —— 不在
        递归的 child keys 里 —— 主体内容整体丢失。这里改用
        ``interactive_card_to_markdown``（见 card_markdown）完整渲染，
        直接收到的卡片消息与 quoted 路径（经
        ``_process_quoted_message``）都受益。渲染失败（JSON 损坏等）
        回退父类单行压平。
        """
        if msg_type == "interactive":
            markdown = await interactive_card_to_markdown(
                content_raw,
                at_resolver=self._get_user_name_by_open_id,
            )
            if markdown:
                # interactive 卡片无媒体 content_parts，([], []) 与
                # 父类一致。
                return markdown, [], []
        return await super()._parse_message_content(
            msg_type,
            content_raw,
            message_id,
        )

    # ------------------------------------------------------------------
    # _process_quoted_message —— 命令跳过引用 + interactive 引用块
    # ------------------------------------------------------------------

    async def _process_quoted_message(  # type: ignore[override]
        self,
        parent_id: str,
        text_parts: List[str],
        content_parts: List[Any],
    ) -> None:
        """处理被引用（回复）消息：interactive 卡片渲染为 ``> `` 引用块，
        其余类型保持父类拼装；以 ``/`` 开头的消息跳过引用获取。

        父类 ``_on_message`` 在调用本方法前已把当前消息正文放在
        ``text_parts[0]``（mention key 已剥离），据此判断是否为命令。
        若不跳过，用户回复机器人 interactive 卡片（流式卡片 / 审批
        卡片）输入 ``/xxx`` 时，引用的卡片正文会被前置成
        ``[quoted interactive: ...]``，命令文本不再以 ``/`` 开头，
        command_registry 的前缀匹配随之失效。跳过意味着不发
        Get Message 请求，引用内容对命令场景本就无意义。

        interactive 卡片经 ``_parse_message_content`` 覆写得到完整
        Markdown，以 markdown 引用块（每行 ``> `` 前缀）前置，与用户
        正文空行分隔。非 interactive 类型复刻父类拼装（单行
        ``[quoted {label}: ...]`` + error hints + media content_parts），
        fetch 只做一次，不再透传 super 导致二次 Get Message。
        """
        first = text_parts[0].strip() if text_parts else ""
        if first.startswith("/"):
            logger.debug(
                "feishu-plus skip quoted message for slash command: %s",
                first[:30],
            )
            return

        result = await self._fetch_quoted_message_content(parent_id)
        if not result:
            return
        quoted_msg_type, quoted_content = result
        logger.info(
            "feishu-plus quoted message: parent_id=%s type=%s",
            parent_id[:20],
            quoted_msg_type,
        )

        (
            main_text,
            error_hints,
            parsed_content,
        ) = await self._parse_message_content(
            quoted_msg_type,
            quoted_content,
            parent_id,
        )

        if quoted_msg_type == "interactive" and main_text:
            text_parts[:0] = [_quote_block(main_text)]
            return

        # 非 interactive：父类拼装逻辑（label 单行 + hints + media）。
        label = _MSG_TYPE_LABEL.get(quoted_msg_type, quoted_msg_type)
        quoted_lines: List[str] = []
        if main_text:
            quoted_lines.append(f"[quoted {label}: {main_text}]")
        else:
            quoted_lines.append(f"[quoted {label}]")
        for hint in error_hints:
            quoted_lines.append(
                (
                    f"[quoted {hint[1:]}"
                    if hint.startswith("[")
                    else f"[quoted {hint}]"
                ),
            )
        text_parts[:0] = quoted_lines

        content_parts.extend(parsed_content)

    # ==================================================================
    # 话题内流式输出（实验性）
    #
    # 父类在三处跳过话题流式，注释称 "Feishu threads lack streaming
    # (card update) capability"。CardKit 流式 = ① card.create 得 card_id
    # ② 发 interactive 消息引用 card_id ③ card_element.content 流式更新。
    # 仅 ② 受话题影响，这里让它在话题内改走 _reply_in_thread。
    # 若飞书话题不支持，_create_streaming_card 返回 None，
    # on_streaming_end 会自动回退为纯文本回复，不影响功能。
    # ==================================================================

    async def _create_streaming_card(  # type: ignore[override]
        self,
        receive_id_type: str,
        receive_id: str,
        initial_text: str = "...",
        thread_msg_id: str = "",
    ) -> Optional[Dict[str, str]]:
        """创建 CardKit 流式卡片并发送。

        ``thread_msg_id`` 非空时，卡片消息通过 ``_reply_in_thread``
        发到话题内；否则走父类原路径发到会话根。
        """
        if not self._client:
            return None

        from lark_oapi.api.cardkit.v1 import (
            CreateCardRequest,
            CreateCardRequestBody,
        )

        element_id = FEISHU_STREAM_ELEMENT_ID
        card_json = {
            "schema": "2.0",
            "config": {"streaming_mode": True},
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": initial_text,
                        "element_id": element_id,
                    },
                ],
            },
        }

        # ① 创建卡片资源（与话题无关）
        try:
            create_req = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(card_json, ensure_ascii=False))
                    .build(),
                )
                .build()
            )
            create_resp = await self._client.cardkit.v1.card.acreate(
                create_req,
            )
            if not create_resp.success():
                logger.warning(
                    "feishu-plus create streaming card failed: "
                    "code=%s msg=%s",
                    create_resp.code,
                    create_resp.msg,
                )
                return None
            card_id = (
                getattr(create_resp.data, "card_id", None)
                if create_resp.data
                else None
            )
            if not card_id:
                logger.warning(
                    "feishu-plus create streaming card: no card_id",
                )
                return None
        except Exception:
            logger.exception("feishu-plus create streaming card failed")
            return None

        # ② 发送卡片消息：话题内 reply，否则发到根
        try:
            msg_content = json.dumps(
                {"type": "card", "data": {"card_id": card_id}},
                ensure_ascii=False,
            )
            if thread_msg_id:
                message_id = await self._reply_in_thread(
                    thread_msg_id,
                    "interactive",
                    msg_content,
                )
            else:
                message_id = await self._send_message(
                    receive_id_type,
                    receive_id,
                    "interactive",
                    msg_content,
                )
            if not message_id:
                logger.warning(
                    "feishu-plus streaming card: send failed card_id=%s "
                    "thread=%s",
                    card_id,
                    bool(thread_msg_id),
                )
                return None
            return {"card_id": card_id, "message_id": message_id}
        except Exception:
            logger.warning(
                "feishu-plus streaming card: send exception",
                exc_info=True,
            )
            return None

    async def on_streaming_start(  # type: ignore[override]
        self,
        request: Any,
        to_handle: str,
        event: Any,
        send_meta: Dict[str, Any],
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        """话题内也创建流式卡片（不再 return 跳过）。"""
        if not self.streaming_enabled:
            return
        # 父类在此处有 `if send_meta.get("feishu_thread_id"): return`
        # —— 本增强移除该跳过。
        recv = await self._get_receive_for_send(to_handle, send_meta)
        if not recv:
            return

        receive_id_type, receive_id = recv
        state = self._get_feishu_stream_state(send_meta)

        thread_msg_id = ""
        if send_meta.get("feishu_thread_id"):
            thread_msg_id = send_meta.get("feishu_message_id", "")

        # 复用预创建的卡片（话题时由 _before_consume_process 预创建）
        card_info = getattr(request, "_precreated_card", None)
        if card_info:
            setattr(request, "_precreated_card", None)
        else:
            initial = self._build_stream_display_text(
                stream_type,
                "...",
                send_meta,
            )
            card_info = await self._create_streaming_card(
                receive_id_type,
                receive_id,
                initial_text=initial,
                thread_msg_id=thread_msg_id,
            )

        if card_info:
            state["cards"][stream_type] = {
                "card_id": card_info["card_id"],
                "message_id": card_info["message_id"],
                "sequence": 0,
            }

    async def _before_consume_process(self, request: Any) -> None:
        """话题内也预创建流式卡片（不再跳过）。"""
        meta = getattr(request, "channel_meta", None) or {}
        receive_id = meta.get("feishu_receive_id")
        receive_id_type = meta.get("feishu_receive_id_type", "open_id")
        if receive_id and getattr(request, "session_id", None):
            await self._save_receive_id(
                request.session_id,
                receive_id,
                receive_id_type,
            )

        # 预创建流式卡片；与父类的区别：不再因为 feishu_thread_id 跳过，
        # 并在话题时把 feishu_message_id 透传给 _reply_in_thread。
        if (
            self.streaming_enabled
            and receive_id
            and not meta.get("from_card_action")
        ):
            thread_msg_id = ""
            if meta.get("feishu_thread_id"):
                thread_msg_id = meta.get("feishu_message_id", "")
            try:
                card_info = await self._create_streaming_card(
                    receive_id_type,
                    receive_id,
                    initial_text="...",
                    thread_msg_id=thread_msg_id,
                )
                if card_info:
                    setattr(request, "_precreated_card", card_info)
            except Exception:
                logger.debug(
                    "feishu-plus streaming card pre-creation failed",
                    exc_info=True,
                )
