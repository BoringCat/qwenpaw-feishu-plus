# -*- coding: utf-8 -*-
"""FeishuPlusChannel —— 继承内置 FeishuChannel 的增强飞书渠道。

channel key = ``feishu_plus``（独立 key，避免与内置 feishu 冲突而被
registry 跳过）。全部收发 / WebSocket / CardKit / 媒体能力继承自父类，
本类只做四件事：

1. 覆盖 tool-guard 审批卡片的 render —— 话题内走 ``_reply_in_thread``
   + ``msg_type="interactive"``（见 card.override）。
2. 放开话题内流式输出的三处跳过（实验性，依赖飞书话题对 CardKit
   interactive 卡片的支持；失败自动回退纯文本）。以 ``/`` 开头的
   命令消息除外 —— 不创建流式卡片，回复始终以纯文本发出（见
   ``_before_consume_process`` / ``on_streaming_start``）。
3. 以 ``/`` 开头的消息（控制命令）跳过引用消息获取 —— 用户回复
   机器人卡片时输入命令，父类抓回引用的 interactive 卡片内容并前置
   ``[quoted interactive: ...]``，会让命令文本不再以 ``/`` 开头。
4. interactive 卡片渲染为结构化 Markdown（见 card.markdown）——
   父类 ``extract_interactive_text`` 会把卡片压成单行，且 CardKit v2
   ``div`` 的正文（在 ``text.content`` 键）整体丢失；本类对直接收到的
   interactive 消息与被引用（quoted）卡片都改为完整 Markdown 渲染，
   quoted 时以 ``> `` 引用块前置。
5. 触发规则（YAML 文件配置）—— 群消息正文满足规则的 bool 条件组
   （``must`` 全部命中、``must_not`` 全部不命中、``should`` 至少
   ``minimum_should_match`` 个命中；每条条件 ``regex`` 正则或
   ``keyword`` 字面子串二选一）时绕过 ``require_mention`` 的 @提及
   检查；规则可携带 ``context``（命中时追加到消息正文末尾一并发送
   给 AI）与 ``chat_ids``（按群 chat_id 白名单限定，缺省全部群生效），
   见 ``Trigger.load``。匹配对象为
   text 消息正文与 interactive 卡片渲染出的 Markdown（卡片无 text
   字段可改写，``context`` 经 ``_trigger_context.context`` 传递、由
   ``_parse_message_content`` 在渲染末尾追加）。
6. 自动进话题 —— 触发规则命中且消息不在话题中时，向事件注入
   ``thread_id = message_id``，父类话题管道（session 聚合 / 话题回复 /
   流式卡片）全部自动复用（见 ``_on_message``）。
7. ``/feishu-plus`` 管理命令 —— 经 ``plugin.py`` 以
   ``register_slash_command`` 注册，handler 从 ctx 定位本渠道实例后
   执行子命令：``show-triggers``（查看当前触发配置）与
   ``reload-triggers``（重新加载规则 YAML），见 ``describe_triggers`` /
   ``reload_triggers``。
"""
from __future__ import annotations

import json
import logging
import typing as _t

from pathlib import Path
from contextvars import ContextVar, Token
from types import SimpleNamespace

from qwenpaw.app.channels.feishu.channel import FeishuChannel, _MSG_TYPE_LABEL
from qwenpaw.app.channels.feishu.constants import FEISHU_STREAM_ELEMENT_ID
from qwenpaw.app.channels.feishu.utils import short_session_id_from_full_id
from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.app.channels.base import ProcessHandler, OnReplySent
from qwenpaw.schemas import AgentRequest
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from .card.markdown import (
    interactive_card_to_markdown,
    quote_block,
)
from .trigger import Trigger, TriggerContext, TRIGGER_YAML_DEFAULT_NAME

logger = logging.getLogger(__name__)

# request 动态属性名：以 ``/`` 开头的命令消息本次请求跳过流式输出。
# ``_before_consume_process`` 在 agent 运行前判定并置位，
# ``on_streaming_start`` 在事件循环中读取 —— 两者共享同一 AgentRequest
# 实例（与父类 ``_precreated_card`` 的跨方法传递方式一致）。
_NO_STREAMING_REQUEST_ATTR = "_feishu_plus_no_streaming"

class FeishuPlusChannel(FeishuChannel):
    """话题感知的飞书渠道。"""

    channel = "feishu_plus"

    def __init__(self, *args: _t.Any, **kwargs: _t.Any) -> None:
        super().__init__(*args, **kwargs)
        # 父类 __init__ 已创建 self._card_handler（FeishuCardHandler）。
        # 用 dispatcher 的 public register() 覆盖 tool_guard 的 CardKind，
        # 把 render + handle 都换成话题感知版本（见 card.override），
        # 这样恢复 site-packages 的 tool_guard.py / context.py 上游版本后，
        # 插件仍完整具备话题能力。
        from qwenpaw.app.channels.feishu.cards import tool_guard
        from qwenpaw.app.channels.feishu.cards.dispatcher import CardKind

        from .card.override import (
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

        # ── 触发规则（from_config 覆盖；见 Trigger.load） ──
        self._trigger = Trigger()

    # ------------------------------------------------------------------
    # from_config —— 插件频道的 config 是 SimpleNamespace（非 Pydantic）
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        process: ProcessHandler,
        config: SimpleNamespace,
        on_reply_sent: OnReplySent = None,
        display_config: ChannelDisplayConfig|None = None,
        no_text_debounce: bool = True,
        workspace_dir: Path|None = None,
    ) -> "FeishuPlusChannel":
        """从 SimpleNamespace 配置创建实例。

        插件频道的 ``config`` 是 ``types.SimpleNamespace``，必须用
        ``getattr`` 安全读取（详见官方插件文档「示例 10」）。
        """
        channel = cls(
            process            = process,
            enabled            = bool(getattr(config, "enabled", False)),
            app_id             = getattr(config, "app_id", "") or "",
            app_secret         = getattr(config, "app_secret", "") or "",
            bot_prefix         = getattr(config, "bot_prefix", "") or "",
            encrypt_key        = getattr(config, "encrypt_key", "") or "",
            verification_token = getattr(config, "verification_token", "") or "",
            media_dir          = getattr(config, "media_dir", "") or "",
            workspace_dir      = workspace_dir,
            on_reply_sent      = on_reply_sent,
            display_config     = (
                display_config or
                ChannelDisplayConfig.from_config(config)
            ),
            no_text_debounce       = no_text_debounce,
            dm_policy              = getattr(config, "dm_policy", "open") or "open",
            group_policy           = getattr(config, "group_policy", "open") or "open",
            allow_from             = getattr(config, "allow_from", None) or [],
            deny_message           = getattr(config, "deny_message", "") or "",
            require_mention        = bool(getattr(config, "require_mention", False)),
            domain                 = getattr(config, "domain", "feishu") or "feishu",
            streaming_enabled      = bool(getattr(config, "streaming_enabled", False)),
            share_session_in_group = bool(
                getattr(config, "share_session_in_group", False),
            ),
            access_control_dm      = bool(
                getattr(config, "access_control_dm", False),
            ),
            access_control_group   = bool(
                getattr(config, "access_control_group", False),
            ),
        )
        # 父类 __init__ 签名固定，插件专属配置在此以属性注入。
        channel._trigger.auto_thread = bool(
            getattr(config, "auto_thread_on_trigger", False),
        )
        ws_dir      = workspace_dir or Path.cwd()
        custom_path = str(
            getattr(config, "trigger_yaml_path", "") or "",
        ).strip()
        if custom_path:
            yaml_path = Path(custom_path)
            if not yaml_path.is_absolute():
                yaml_path = ws_dir / yaml_path
        else:
            yaml_path = ws_dir / TRIGGER_YAML_DEFAULT_NAME
        channel._trigger.config_file = str(yaml_path)
        channel._trigger.load()
        return channel

    # ------------------------------------------------------------------
    # /feishu-plus 管理命令支持 —— show-triggers / reload-triggers
    # ------------------------------------------------------------------

    def describe_triggers(self) -> str:
        return self._trigger.describe_triggers()

    def reload_triggers(self) -> str:
        return self._trigger.reload_triggers()

    async def _on_message(  # type: ignore[override]
        self,
        data: 'P2ImMessageReceiveV1',
    ) -> None:
        """触发规则命中时注入话题 + 追加 context，再走父类。

        命中后做三件事（全部通过改写 event 数据完成，父类流程无感知）：

        1. ``auto_thread_on_trigger`` 开启且消息无 ``thread_id`` 时注入
           ``thread_id = message_id`` —— 父类话题管道（session 按
           thread 聚合、``_reply_in_thread`` 话题回复、流式卡片进话题）
           全部自动复用；飞书话题根消息的 thread_id 即其自身
           message_id，后续话题内消息携带相同值，会话天然连续。
        2. 规则携带 ``context`` 时追加到发给 AI 的正文末尾：text 消息
           改写 ``content`` 的 text 字段（与 @触发 时正文直接可见的
           效果一致，quoted 引用块仍前置、slash 命令前缀判断不受
           末尾追加影响）；interactive 卡片无 text 字段可改写，经
           ``_trigger.context.context`` 由 ``_parse_message_content`` 覆写在
           渲染 Markdown 末尾追加。
        3. 置 ``_trigger.context.matched`` 供 ``_check_group_mention`` 覆写
           读取（require_mention 场景绕过 @提及 检查）。
        """
        if not data or not getattr(data, "event", None):
            return
        tokens:dict[ContextVar, Token] = {}
        try:
            event   = data.event
            message = getattr(event, "message", None)
            matched = False
            context = ""
            if message is not None:
                matched, context = await self._trigger.match(message)
            if not matched:
                await super()._on_message(data)
                return

            message_id = getattr(message, "message_id", None) or ""
            if self._trigger.auto_thread and message_id:
                thread_id = str(
                    getattr(message, "thread_id", "") or "",
                ).strip()
                if not thread_id:
                    message.thread_id = message_id
                    logger.info(
                        "feishu-plus trigger auto-thread: msg=%s",
                        message_id[:20],
                    )
            if context:
                message_type = str(
                    getattr(message, "message_type", "") or "",
                ).strip()
                if message_type == "interactive":
                    # 卡片无 text 字段可改写：经 context
                    # 交给 _parse_message_content
                    # 覆写在渲染 Markdown 末尾追加。
                    tokens[self._trigger.context.message_id] = (
                        self._trigger.context.message_id.set(message_id)
                    )
                    tokens[self._trigger.context.context] = (
                        self._trigger.context.context.set(context)
                    )
                else:
                    try:
                        payload = json.loads(
                            getattr(message, "content", None) or "{}",
                        )
                        text = str((payload or {}).get("text", "") or "")
                        payload["text"] = (
                            f"{text}\n{context}" if text else context
                        )
                        message.content = json.dumps(
                            payload,
                            ensure_ascii=False,
                        )
                    except (ValueError, TypeError):
                        logger.warning(
                            "feishu-plus trigger: append context failed, "
                            "raw content kept",
                        )
            tokens[self._trigger.context.matched] = (
                self._trigger.context.matched.set(True)
            )
            await super()._on_message(data)
        except Exception:
            logger.exception("feishu plus _on_message failed")
        finally:
            for k, t in tokens.items():
                k.reset(t)

    def _check_group_mention(  # type: ignore[override]
        self,
        is_group: bool,
        meta: dict[str, _t.Any],
    ) -> bool:
        """触发规则命中时绕过 @提及 检查，其余透传父类。

        ``_TRIGGER_MATCHED`` 仅在 ``_on_message`` wrapper 内群消息
        命中时置位（同一 task 直接 await，无并发串扰），p2p 路径
        恒为 False，不受影响。
        """
        if self._trigger.context.matched.get():
            return True
        return super()._check_group_mention(is_group, meta)

    # ------------------------------------------------------------------
    # resolve_session_id —— 话题感知的 session 聚合
    # ------------------------------------------------------------------

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: dict[str, _t.Any]|None = None,
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
    ) -> tuple[str|None, list[str], list[_t.Any]]:
        """interactive 消息渲染为结构化 Markdown（其余类型透传父类）。

        父类对 interactive 调 ``extract_interactive_text``：把卡片压成
        单行，且 CardKit v2 ``div`` 正文在 ``text.content`` 键 —— 不在
        递归的 child keys 里 —— 主体内容整体丢失。这里改用
        ``interactive_card_to_markdown``（见 card.markdown）完整渲染，
        直接收到的卡片消息与 quoted 路径（经
        ``_process_quoted_message``）都受益。渲染失败（JSON 损坏等）
        回退父类单行压平。

        触发规则命中（interactive）时，``_on_message`` 经
        ``_trigger_context.context`` 传入规则 context —— 在渲染 Markdown 末尾
        追加一行；``message_id`` 与触发消息一致才追加（quoted 路径
        传 parent_id，不会误吞）。
        """
        if msg_type == "interactive":
            markdown = await interactive_card_to_markdown(
                content_raw,
                at_resolver=self._get_user_name_by_open_id,
            )
            if markdown:
                trig_id = self._trigger.context.message_id.get()
                trig_ctx = self._trigger.context.context.get()
                if trig_ctx and trig_id == message_id:
                    markdown = f"{markdown}\n{trig_ctx}"
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
        text_parts: list[str],
        content_parts: list[_t.Any],
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
            text_parts[:0] = [quote_block(main_text)]
            return

        # 非 interactive：父类拼装逻辑（label 单行 + hints + media）。
        label = _MSG_TYPE_LABEL.get(quoted_msg_type, quoted_msg_type)
        quoted_lines: list[str] = []
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
    ) -> dict[str, str]|None:
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
        request: _t.Any,
        to_handle: str,
        event: _t.Any,
        send_meta: dict[str, _t.Any],
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        """话题内也创建流式卡片（不再 return 跳过）；以 ``/`` 开头的
        命令消息除外 —— 返回后不创建任何卡片，``on_streaming_end`` 会以
        纯文本回退发送完整结果，命令回复保持非流式。
        """
        if not self.streaming_enabled:
            return
        # 命令消息（_before_consume_process 已置标记）不创建流式卡片。
        if getattr(request, _NO_STREAMING_REQUEST_ATTR, False):
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

    def _request_is_slash_command(self, request: AgentRequest) -> bool:
        """request 的用户正文是否以 ``/`` 开头（命令消息）。

        复用 base 的 ``_extract_query_from_payload`` 提取首段 query 文本
        （父类 ``_on_message`` 已剥离 mention key，命令消息也跳过引用
        获取，正文以 ``/`` 开头即命令），判定与 ``_process_quoted_message``
        一致。
        """
        query = self._extract_query_from_payload(request) or ""
        return query.strip().startswith("/")

    async def _before_consume_process(self, request: AgentRequest) -> None:
        """话题内也预创建流式卡片（不再跳过）；以 ``/`` 开头的命令
        消息除外 —— 置 ``_NO_STREAMING_REQUEST_ATTR`` 并跳过预创建，
        ``on_streaming_start`` 读到标记后不再创建，结果以纯文本发送。
        """
        meta = getattr(request, "channel_meta", None) or {}
        receive_id = meta.get("feishu_receive_id")
        receive_id_type = meta.get("feishu_receive_id_type", "open_id")
        if receive_id and getattr(request, "session_id", None):
            await self._save_receive_id(
                request.session_id,
                receive_id,
                receive_id_type,
            )

        # 命令消息（用户正文以 / 开头）不流式输出：跳过预创建并标记
        # request，供 on_streaming_start 复用同一判定。与
        # _process_quoted_message 对 / 命令跳过引用获取是同一语义
        # （正文同样已剥离 mention key）。
        is_slash_command = self._request_is_slash_command(request)
        if is_slash_command:
            setattr(request, _NO_STREAMING_REQUEST_ATTR, True)

        # 预创建流式卡片；与父类的区别：不再因为 feishu_thread_id 跳过，
        # 并在话题时把 feishu_message_id 透传给 _reply_in_thread。
        if (
            self.streaming_enabled
            and receive_id
            and not meta.get("from_card_action")
            and not is_slash_command
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

