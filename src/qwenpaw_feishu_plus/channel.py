# -*- coding: utf-8 -*-
"""FeishuPlusChannel —— 继承内置 FeishuChannel 的增强飞书渠道。

channel key = ``feishu_plus``（独立 key，避免与内置 feishu 冲突而被
registry 跳过）。全部收发 / WebSocket / CardKit / 媒体能力继承自父类，
本类只做四件事：

1. 覆盖 tool-guard 审批卡片的 render —— 话题内走 ``_reply_in_thread``
   + ``msg_type="interactive"``（见 cards_override）。
2. 放开话题内流式输出的三处跳过（实验性，依赖飞书话题对 CardKit
   interactive 卡片的支持；失败自动回退纯文本）。以 ``/`` 开头的
   命令消息除外 —— 不创建流式卡片，回复始终以纯文本发出（见
   ``_before_consume_process`` / ``on_streaming_start``）。
3. 以 ``/`` 开头的消息（控制命令）跳过引用消息获取 —— 用户回复
   机器人卡片时输入命令，父类抓回引用的 interactive 卡片内容并前置
   ``[quoted interactive: ...]``，会让命令文本不再以 ``/`` 开头。
4. interactive 卡片渲染为结构化 Markdown（见 card_markdown）——
   父类 ``extract_interactive_text`` 会把卡片压成单行，且 CardKit v2
   ``div`` 的正文（在 ``text.content`` 键）整体丢失；本类对直接收到的
   interactive 消息与被引用（quoted）卡片都改为完整 Markdown 渲染，
   quoted 时以 ``> `` 引用块前置。
5. 正则触发规则（YAML 文件配置）—— 群消息正文命中任一正则时绕过
   ``require_mention`` 的 @提及 检查；规则可携带 ``context``（命中时
   追加到消息正文末尾一并发送给 AI）与 ``chat_ids``（按群 chat_id
   白名单限定，缺省全部群生效），见 ``_load_trigger_yaml``。
6. 自动进话题 —— 正则触发且消息不在话题中时，向事件注入
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
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from qwenpaw.app.channels.feishu.channel import FeishuChannel, _MSG_TYPE_LABEL
from qwenpaw.app.channels.feishu.constants import FEISHU_STREAM_ELEMENT_ID
from qwenpaw.app.channels.feishu.utils import short_session_id_from_full_id
from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.runtime.hooks import HookContext
from qwenpaw.app.workspace.workspace import Workspace
from qwenpaw.app.channels.manager import ChannelManager
from agentscope.message import Msg

from .card_markdown import interactive_card_to_markdown, quote_lines

logger = logging.getLogger(__name__)

# 正则触发命中标记：``_on_message`` wrapper 在调用父类前置位，父类
# 末尾的 ``_check_group_mention`` 覆写读取。同一 asyncio task 内直接
# ``await super()``，context 对子调用可见且无并发串扰。
_TRIGGER_MATCHED: ContextVar[bool] = ContextVar(
    "feishu_plus_trigger_matched",
    default=False,
)

# 触发规则 YAML 的默认文件名（位于 workspace 根目录下）。
_TRIGGER_YAML_DEFAULT_NAME = "feishu_plus_triggers.yaml"

# ``/feishu-plus`` 命令（show-triggers / reload-triggers）的用法说明，
# 无参数或未知子命令时展示给用户。
_TRIGGER_COMMAND_USAGE = (
    "feishu-plus 触发规则管理命令\n\n"
    "用法:\n"
    "  /feishu-plus show-triggers      查看当前生效的触发规则配置\n"
    "  /feishu-plus reload-triggers    重新加载规则 YAML 文件"
)

# request 动态属性名：以 ``/`` 开头的命令消息本次请求跳过流式输出。
# ``_before_consume_process`` 在 agent 运行前判定并置位，
# ``on_streaming_start`` 在事件循环中读取 —— 两者共享同一 AgentRequest
# 实例（与父类 ``_precreated_card`` 的跨方法传递方式一致）。
_NO_STREAMING_REQUEST_ATTR = "_feishu_plus_no_streaming"


# ── 触发规则 YAML 的 pydantic 模型 ──
#
# ``_load_trigger_yaml`` 先用 ``yaml.safe_load`` 读文件，再用
# ``TriggerRulesFile`` 校验结构。结构 / 类型错误（多余字段、非字符串
# pattern、非列表 triggers 等）会整份置空 —— 配置错误应显式暴露，
# 而不是静默丢弃某条规则；正则本身编译失败仍逐条跳过（字符串在
# 结构上合法，只是正则非法），见 ``_load_trigger_yaml``。


class TriggerRule(BaseModel):
    """单条触发规则：正则 ``pattern``（必填）+ 可选 ``context`` /
    ``chat_ids``。"""

    model_config = ConfigDict(extra="forbid")

    pattern: str
    context: str = ""
    # 限定规则生效的群（chat_id 白名单）；空 = 全部群生效。
    chat_ids: List[str] = Field(default_factory=list)

    @field_validator("pattern", mode="before")
    @classmethod
    def _normalize_pattern(cls, value: Any) -> Any:
        """去首尾空白；去空后为空则报错（pattern 必填）。"""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("pattern must not be empty")
        return value

    @field_validator("context", mode="before")
    @classmethod
    def _normalize_context(cls, value: Any) -> Any:
        """context 去首尾空白；``null`` 视作空串（非字符串报类型错误）。"""
        if value is None:
            return ""
        return value.strip() if isinstance(value, str) else value

    @field_validator("chat_ids", mode="before")
    @classmethod
    def _chat_ids_null_is_empty(cls, value: Any) -> Any:
        """``null`` 视作空列表（不限群）；非列表由类型校验报错。"""
        if value is None:
            return []
        return value

    @field_validator("chat_ids", mode="after")
    @classmethod
    def _normalize_chat_ids(cls, value: List[str]) -> List[str]:
        """chat_ids 逐项去首尾空白；空条目报错（不该出现的配置）。"""
        cleaned = []
        for item in value:
            item = item.strip()
            if not item:
                raise ValueError("chat_ids entries must not be empty")
            cleaned.append(item)
        return cleaned


class TriggerRulesFile(BaseModel):
    """触发规则 YAML 顶层结构：``triggers`` 规则列表。"""

    model_config = ConfigDict(extra="forbid")

    triggers: List[TriggerRule]


class _CompiledTrigger(NamedTuple):
    """编译好的单条规则：pattern + context + chat_ids（空 = 全部群）。

    ``_trigger_rules`` 的元素类型；NamedTuple 便于测试按索引断言
    （[0] pattern / [1] context / [2] chat_ids）。
    """

    pattern: re.Pattern
    context: str = ""
    chat_ids: Tuple[str, ...] = ()


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

        # ── 正则触发规则（from_config 覆盖；见 _load_trigger_yaml） ──
        self._trigger_rules: List[_CompiledTrigger] = []
        self._trigger_yaml_path: str = ""
        self._auto_thread_on_trigger: bool = False
        # 最近一次 yaml 加载的结果状态，供 /feishu-plus 命令向用户反馈
        # （_load_trigger_yaml 每次调用先复位再按失败分支填充）。
        self._trigger_load_error: str = ""
        # 最近一次加载中被跳过（正则非法）的规则条数。
        self._trigger_skipped: int = 0

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
        channel = cls(
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
        # 父类 __init__ 签名固定，插件专属配置在此以属性注入。
        channel._auto_thread_on_trigger = bool(
            getattr(config, "auto_thread_on_trigger", False),
        )
        ws_dir = workspace_dir or Path.cwd()
        custom_path = str(
            getattr(config, "trigger_yaml_path", "") or "",
        ).strip()
        if custom_path:
            yaml_path = Path(custom_path)
            if not yaml_path.is_absolute():
                yaml_path = ws_dir / yaml_path
        else:
            yaml_path = ws_dir / _TRIGGER_YAML_DEFAULT_NAME
        channel._trigger_yaml_path = str(yaml_path)
        channel._load_trigger_yaml(yaml_path)
        return channel

    # ------------------------------------------------------------------
    # 正则触发规则 —— YAML 加载 / 匹配 / mention 绕过 / 自动进话题
    # ------------------------------------------------------------------

    def _load_trigger_yaml(self, path: Path) -> None:
        """加载触发规则 YAML（顶层 ``triggers:`` 列表）。

        文件结构由 ``TriggerRulesFile`` / ``TriggerRule`` 两个 pydantic
        模型校验：``triggers`` 为规则列表，每条 ``pattern``（正则，
        ``re.search`` 语义）为必填非空字符串、``context`` 与 ``chat_ids``
        可选（后者为群 chat_id 白名单，空 = 全部群生效），多余字段与
        类型错误整份置空。正则编译失败逐条跳过并告警，不影响其余条目。
        文件不存在 / 解析失败 / 结构不符只记日志并置空规则，不抛异常
        —— 触发规则是增强能力，不应因配置问题阻断渠道启动。

        调用副作用（供 ``/feishu-plus`` 命令反馈）：失败时置
        ``self._trigger_load_error`` 为人类可读原因（成功 / 默认文件缺失
        则保持空串），被跳过的非法正则条数写入 ``self._trigger_skipped``。
        """
        self._trigger_rules = []
        self._trigger_load_error = ""
        self._trigger_skipped = 0
        if not path or not path.is_file():
            if path is not None and str(path).endswith(
                _TRIGGER_YAML_DEFAULT_NAME,
            ):
                logger.info(
                    "feishu-plus trigger yaml not found (ok): %s", path,
                )
            else:
                logger.warning(
                    "feishu-plus trigger yaml not found: %s", path,
                )
                self._trigger_load_error = f"文件不存在: {path}"
            return
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.warning(
                "feishu-plus trigger yaml parse failed: %s",
                path,
                exc_info=True,
            )
            self._trigger_load_error = "文件解析失败（非法 YAML）"
            return
        try:
            rules_file = TriggerRulesFile.model_validate(data)
        except ValidationError as exc:
            logger.warning(
                "feishu-plus trigger yaml invalid format: %s (%s)",
                path,
                exc,
            )
            # ValidationError 消息多行且含 schema 定位，首行即可作
            # 命令反馈（完整信息在日志中）。
            first_line = str(exc).splitlines()[0] if exc.errors() else str(exc)
            self._trigger_load_error = f"文件结构非法: {first_line[:120]}"
            return
        skipped = 0
        for idx, rule in enumerate(rules_file.triggers):
            try:
                pattern = re.compile(rule.pattern)
            except re.error as exc:
                skipped += 1
                logger.warning(
                    "feishu-plus trigger yaml: rule #%d invalid regex "
                    "%r (%s), skipped",
                    idx,
                    rule.pattern,
                    exc,
                )
                continue
            self._trigger_rules.append(
                _CompiledTrigger(pattern, rule.context, tuple(rule.chat_ids)),
            )
        if skipped:
            self._trigger_skipped = skipped
        logger.info(
            "feishu-plus trigger rules loaded: %d from %s",
            len(self._trigger_rules),
            path,
        )

    # ------------------------------------------------------------------
    # /feishu-plus 管理命令支持 —— show-triggers / reload-triggers
    # ------------------------------------------------------------------

    def describe_triggers(self) -> str:
        """生成当前触发规则配置的人类可读文本（show-triggers）。

        列出规则文件路径、相关触发开关与逐条生效规则（pattern /
        context / chat_ids 白名单）。规则为空时给出原因（文件缺失 /
        加载失败 / 文件中没有规则），便于运维定位。
        """
        rules = list(getattr(self, "_trigger_rules", None) or [])
        path = str(getattr(self, "_trigger_yaml_path", "") or "")
        error = str(getattr(self, "_trigger_load_error", "") or "")
        auto_thread = bool(getattr(self, "_auto_thread_on_trigger", False))

        lines = [
            f"feishu-plus 触发规则（生效 {len(rules)} 条）",
            f"规则文件: {path or '（未配置 trigger_yaml_path）'}",
            f"auto_thread_on_trigger: {'开' if auto_thread else '关'}",
        ]
        if error:
            lines.append(f"最近一次加载失败: {error}")
        elif not rules:
            if not path or not Path(path).is_file():
                lines.append("状态: 规则文件不存在，未配置任何触发规则")
            else:
                lines.append("状态: 规则文件中没有生效的触发规则")
        for idx, rule in enumerate(rules, start=1):
            scope = (
                "全部群"
                if not rule.chat_ids
                else "群白名单: " + "、".join(rule.chat_ids)
            )
            lines.append("")
            lines.append(f"{idx}. pattern: {rule.pattern.pattern}")
            if rule.context:
                lines.append(f"   context: {rule.context}")
            lines.append(f"   生效范围: {scope}")
        if getattr(self, "_trigger_skipped", 0):
            lines.append(
                ""
                f"（本次加载 {self._trigger_skipped} 条非法正则被跳过）",
            )
        return "\n".join(lines)

    def reload_triggers(self) -> str:
        """重新从 ``_trigger_yaml_path`` 加载触发规则并返回可读结果。

        与启动共用 ``_load_trigger_yaml``，改完 YAML 后免重启生效。
        加载失败（文件缺失 / 非法 YAML / 结构错误）时**保留上一份生效
        规则**并回滚 —— 一次格式错误不应清空线上正在工作的规则集。
        """
        path = str(getattr(self, "_trigger_yaml_path", "") or "")
        if not path:
            return "未配置规则文件（trigger_yaml_path 为空），无法重新加载。"
        previous = list(getattr(self, "_trigger_rules", None) or [])
        path_obj = Path(path)
        # 主动 reload 时文件缺失同样视为失败：默认路径缺失在启动时是正常
        # 的「未配置」，但 reload 的语义是「让当前文件生效」，应显式反馈。
        if path_obj.is_file():
            self._load_trigger_yaml(path_obj)
        else:
            self._trigger_rules = []
            self._trigger_load_error = f"文件不存在: {path}"
            self._trigger_skipped = 0
        error = str(getattr(self, "_trigger_load_error", "") or "")
        if error:
            # 回滚到上一份生效规则；error 保留供 describe_triggers 展示。
            self._trigger_rules = previous
            if previous:
                return (
                    f"重新加载失败: {error}\n"
                    f"已回滚，继续使用上一份生效规则（{len(previous)} 条）。\n"
                    "修复 YAML 后可再次执行本命令。"
                )
            return f"重新加载失败: {error}\n当前没有生效的触发规则。"
        skipped = int(getattr(self, "_trigger_skipped", 0) or 0)
        head = f"触发规则已重新加载，生效 {len(self._trigger_rules)} 条。"
        if skipped:
            head += f"（其中 {skipped} 条正则非法被跳过，详见日志）"
        return f"{head}\n\n{self.describe_triggers()}"

    def _match_trigger(
        self,
        message: Any,
    ) -> Tuple[bool, str]:
        """群 text 消息正文匹配触发规则。

        返回 ``(matched, context)``：第一条 ``pattern.search`` 命中且
        群 chat_id 在规则 ``chat_ids`` 白名单内（或规则不限群）的
        context（未配置则为空串）；未命中 / 非群 / 非 text / 正文解析
        失败均返回 ``(False, "")``。仅匹配 text 类型 —— 正则面向文本，
        富文本（post）与媒体消息结构各异，不猜。
        """
        if not self._trigger_rules:
            return False, ""
        if str(
            getattr(message, "chat_type", "p2p") or "p2p",
        ).strip() != "group":
            return False, ""
        if str(
            getattr(message, "message_type", "") or "",
        ).strip() != "text":
            return False, ""
        chat_id = str(getattr(message, "chat_id", "") or "").strip()
        content_raw = getattr(message, "content", None) or ""
        try:
            text = str(
                (json.loads(content_raw) or {}).get("text", "") or "",
            )
        except (ValueError, TypeError):
            return False, ""
        for rule in self._trigger_rules:
            # 规则限定群且当前群不在白名单 → 跳过该规则，继续找下一条。
            if rule.chat_ids and chat_id not in rule.chat_ids:
                continue
            if rule.pattern.search(text):
                return True, rule.context
        return False, ""

    async def _on_message(  # type: ignore[override]
        self,
        data: Any,
    ) -> None:
        """正则触发包装：命中时注入话题 + 追加 context，再走父类。

        命中后做三件事（全部通过改写 event 数据完成，父类流程无感知）：

        1. ``auto_thread_on_trigger`` 开启且消息无 ``thread_id`` 时注入
           ``thread_id = message_id`` —— 父类话题管道（session 按
           thread 聚合、``_reply_in_thread`` 话题回复、流式卡片进话题）
           全部自动复用；飞书话题根消息的 thread_id 即其自身
           message_id，后续话题内消息携带相同值，会话天然连续。
        2. 规则携带 ``context`` 时改写 ``content`` 的 text 字段，末尾
           追加一行 —— 与 @触发 时正文直接可见的效果一致，quoted
           引用块仍前置、slash 命令前缀判断不受末尾追加影响。
        3. 置 ``_TRIGGER_MATCHED`` 供 ``_check_group_mention`` 覆写
           读取（require_mention 场景绕过 @提及 检查）。
        """
        if not data or not getattr(data, "event", None):
            return
        try:
            event   = data.event
            message = getattr(event, "message", None)
            matched = False
            context = ""
            if message is not None:
                matched, context = self._match_trigger(message)
            if not matched:
                await super()._on_message(data)
                return

            message_id = getattr(message, "message_id", None) or ""
            if self._auto_thread_on_trigger and message_id:
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
                try:
                    payload = json.loads(
                        getattr(message, "content", None) or "{}",
                    )
                    text = str((payload or {}).get("text", "") or "")
                    payload["text"] = f"{text}\n{context}" if text else context
                    message.content = json.dumps(
                        payload,
                        ensure_ascii=False,
                    )
                except (ValueError, TypeError):
                    logger.warning(
                        "feishu-plus trigger: append context failed, raw "
                        "content kept",
                    )
            token = _TRIGGER_MATCHED.set(True)
            try:
                await super()._on_message(data)
            finally:
                _TRIGGER_MATCHED.reset(token)
        except Exception:
            logger.exception("feishu plus _on_message failed")

    def _check_group_mention(  # type: ignore[override]
        self,
        is_group: bool,
        meta: Dict[str, Any],
    ) -> bool:
        """正则命中时绕过 @提及 检查，其余透传父类。

        ``_TRIGGER_MATCHED`` 仅在 ``_on_message`` wrapper 内群消息
        命中时置位（同一 task 直接 await，无并发串扰），p2p 路径
        恒为 False，不受影响。
        """
        if _TRIGGER_MATCHED.get():
            return True
        return super()._check_group_mention(is_group, meta)

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

    def _request_is_slash_command(self, request: Any) -> bool:
        """request 的用户正文是否以 ``/`` 开头（命令消息）。

        复用 base 的 ``_extract_query_from_payload`` 提取首段 query 文本
        （父类 ``_on_message`` 已剥离 mention key，命令消息也跳过引用
        获取，正文以 ``/`` 开头即命令），判定与 ``_process_quoted_message``
        一致。
        """
        query = self._extract_query_from_payload(request) or ""
        return query.strip().startswith("/")

    async def _before_consume_process(self, request: Any) -> None:
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


# ======================================================================
# /feishu-plus 管理命令（slash command）handler
#
# ``plugin.py`` 经 ``api.register_slash_command`` 注册到每个 workspace 的
# ``SlashCommandRegistry``；用户文本 ``/feishu-plus <sub>`` 在
# ``Runtime.run`` 的固定命令阶段被 dispatch，剩余文本即 ``args``。handler
# 从 ctx 的 workspace channel_manager 定位当前 ``feishu_plus`` 渠道实例，
# 调用其触发规则支持方法并把结果作为回复 Msg 返回（协议与内置 control
# 命令一致，见 runtime/builtin_commands.py）。
# ======================================================================


def _command_reply(text: str) -> Any:
    """把命令结果文本包装成 slash 命令回复的 agentscope ``Msg``。"""
    from agentscope.message import Msg, TextBlock

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
