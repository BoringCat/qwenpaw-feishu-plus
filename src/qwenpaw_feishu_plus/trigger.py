# -*- coding: utf-8 -*-

# ── 触发规则 YAML 的 pydantic 模型 ──
#
# ``Trigger.load`` 先用 ``yaml.safe_load`` 读文件，再用
# ``TriggerRulesFile`` 校验结构。结构 / 类型错误（多余字段、非字符串
# pattern、非列表 triggers 等）会整份拒绝加载 —— 配置错误应显式暴露，
# 而不是静默丢弃某条规则；正则本身编译失败仍逐条跳过（字符串在
# 结构上合法，只是正则非法），见 ``Trigger.load``。

import json
import logging
import re
import yaml
import typing as _t
from datetime import datetime

from contextvars import ContextVar, Token
from pathlib import Path
from pydantic import BaseModel, ConfigDict, field_validator, ValidationError

from .card.markdown import interactive_card_to_markdown

# 触发规则 YAML 的默认文件名（位于 workspace 根目录下）。
TRIGGER_YAML_DEFAULT_NAME = "feishu_plus_triggers.yaml"

logger = logging.getLogger(__name__)

class TriggerRule(BaseModel):
    """单条触发规则：正则 ``pattern``（必填）+ 可选 ``context`` /
    ``chat_ids``。"""

    model_config = ConfigDict(extra="forbid")

    pattern: str
    context: str = ""
    # 限定规则生效的群（chat_id 白名单）；空 = 全部群生效。
    chat_ids: list[str] = []

    @field_validator("pattern", mode="before")
    @classmethod
    def _normalize_pattern(cls, value: _t.Any) -> _t.Any:
        """去首尾空白；去空后为空则报错（pattern 必填）。"""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("pattern must not be empty")
        return value

    @field_validator("context", mode="before")
    @classmethod
    def _normalize_context(cls, value: _t.Any) -> _t.Any:
        """context 去首尾空白；``null`` 视作空串（非字符串报类型错误）。"""
        if value is None:
            return ""
        return value.strip() if isinstance(value, str) else value

    @field_validator("chat_ids", mode="before")
    @classmethod
    def _chat_ids_null_is_empty(cls, value: _t.Any) ->_t.Any:
        """``null`` 视作空列表（不限群）；非列表由类型校验报错。"""
        if value is None:
            return []
        return value

    @field_validator("chat_ids", mode="after")
    @classmethod
    def _normalize_chat_ids(cls, value: list[str]) -> list[str]:
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

    triggers: list[TriggerRule]

class CompiledTrigger(_t.NamedTuple):
    """编译好的单条规则：pattern + context + chat_ids（空 = 全部群）。

    ``Trigger.rules`` 的元素类型；NamedTuple 便于测试按索引断言
    （[0] pattern / [1] context / [2] chat_ids）。
    """

    pattern: re.Pattern
    context: str = ""
    chat_ids: tuple[str, ...] = ()

class TriggerContext():
    matched: ContextVar[bool] = ContextVar(
        "feishu_plus_trigger_matched",
        default=False,
    )
    message_id: ContextVar[str] = ContextVar(
        "feishu_plus_trigger_message_id",
        default="",
    )
    context: ContextVar[str] = ContextVar(
        "feishu_plus_trigger_context",
        default="",
    )

class Trigger():
    context = TriggerContext()
    def __init__(self,
        rules:list[CompiledTrigger] = [],
        path:str|Path               = '',
        auto_thread:bool            = False,
    ):
        # ── 正则触发规则（from_config 覆盖；见 Trigger.load） ──
        self.__rules       = rules
        self.__path        = Path(path) if path else None
        self.__auto_thread = auto_thread
        # 最近一次 yaml 加载的结果状态，供 /feishu-plus 命令向用户反馈
        # （load 成功时清零，失败时按分支填充）。
        self.__load_error: str = ""
        self.__load_error_time: datetime|None = None
        # 最近一次加载中被跳过（正则非法）的规则条数。
        self.__skipped: int = 0

    @property
    def auto_thread(self):
        return self.__auto_thread
    @auto_thread.setter
    def auto_thread(self, val:bool):
        self.__auto_thread = val

    @property
    def config_file(self):
        return self.__path
    @config_file.setter
    def config_file(self, val:str|Path):
        self.__path = Path(val)

    @property
    def rules(self):
        return self.__rules

    # ------------------------------------------------------------------
    # 正则触发规则 —— YAML 加载 / 匹配 / mention 绕过 / 自动进话题
    # ------------------------------------------------------------------

    def load(self) -> tuple[bool, int]:
        """加载触发规则 YAML（顶层 ``triggers:`` 列表）。

        文件结构由 ``TriggerRulesFile`` / ``TriggerRule`` 两个 pydantic
        模型校验：``triggers`` 为规则列表，每条 ``pattern``（正则，
        ``re.search`` 语义）为必填非空字符串、``context`` 与 ``chat_ids``
        可选（后者为群 chat_id 白名单，空 = 全部群生效），多余字段与
        类型错误整份置空。正则编译失败逐条跳过并告警，不影响其余条目。
        文件不存在 / 解析失败 / 结构不符只记日志并置空规则，不抛异常
        —— 触发规则是增强能力，不应因配置问题阻断渠道启动。

        调用副作用（供 ``/feishu-plus`` 命令反馈）：失败时置
        ``self.__load_error`` 为人类可读原因（成功 / 默认文件缺失
        则保持空串），被跳过的非法正则条数写入 ``self.__skipped``。
        """
        rules:list[CompiledTrigger] = []
        if not self.__path:
            logger.info("feishu-plus 自动触发文件未配置 (ok)")
            return True, 0
        elif not self.__path.is_file():
            if self.__path.name == TRIGGER_YAML_DEFAULT_NAME:
                logger.info("feishu-plus 自动触发文件未配置 (ok)")
                return True, 0
            logger.warning(
                "feishu-plus 自动触发文件不存在: %s", self.__path,
            )
            self.__load_error = f"文件不存在: {self.__path}"
            self.__load_error_time = datetime.now()
            return False, 0

        try:
            with self.__path.open('r', encoding='UTF-8') as f:
                data = yaml.safe_load(f) or {}
        except:
            logger.warning(
                "feishu-plus trigger yaml parse failed: %s",
                self.__path,
                exc_info=True,
            )
            self.__load_error = "文件解析失败（非法 YAML）"
            self.__load_error_time = datetime.now()
            return False, 0
        try:
            rules_file = TriggerRulesFile.model_validate(data)
        except ValidationError as exc:
            logger.warning(
                "feishu-plus trigger yaml invalid format: %s (%s)",
                self.__path,
                exc,
            )
            # ValidationError 消息多行且含 schema 定位，首行即可作
            # 命令反馈（完整信息在日志中）。
            first_line = str(exc).splitlines()[0] if exc.errors() else str(exc)
            self.__load_error = f"文件结构非法: {first_line[:120]}"
            self.__load_error_time = datetime.now()
            return False, 0
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
            rules.append(
                CompiledTrigger(pattern, rule.context, tuple(rule.chat_ids)),
            )
        logger.info(
            "feishu-plus trigger rules loaded: %d from %s",
            len(rules),
            self.__path,
        )
        self.__load_error = ""
        self.__rules      = rules
        self.__skipped    = skipped
        return True, len(rules)

    def describe_triggers(self) -> str:
        """生成当前触发规则配置的人类可读文本（show-triggers）。

        列出规则文件路径、相关触发开关与逐条生效规则（pattern /
        context / chat_ids 白名单）。规则为空时给出原因（文件缺失 /
        加载失败 / 文件中没有规则），便于运维定位。
        """
        lines = [
            f"触发规则：生效 {len(self.__rules)} 条",
            f"规则文件: {self.__path or '（未配置 trigger_yaml_path）'}",
            f"触发到话题: {'开' if self.__auto_thread else '关'}",
        ]
        if self.__load_error:
            lines.append(f"最近一次加载失败: {self.__load_error}")
        elif not self.__rules:
            if not self.__path or not self.__path.is_file():
                lines.append("状态: 规则文件不存在，未配置任何触发规则")
            else:
                lines.append("状态: 规则文件中没有生效的触发规则")
        for idx, rule in enumerate(self.__rules, start=1):
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
        if self.__skipped:
            lines.append(
                ""
                f"（本次加载 {self.__skipped} 条非法正则被跳过）",
            )
        return "\n".join(lines)

    def reload_triggers(self) -> str:
        """重新从 ``config_file`` 加载触发规则并返回可读结果。

        与启动共用 ``load``，改完 YAML 后免重启生效。
        加载失败（文件缺失 / 非法 YAML / 结构错误）时**保留上一份生效
        规则**并回滚 —— 一次格式错误不应清空线上正在工作的规则集。
        """
        ok, count = self.load()
        if not ok:
            return (
                f"重新加载失败: {self.__load_error_time.isoformat()} {self.__load_error}\n"
                f"将不会使用新的规则。修复 YAML 后可再次执行本命令。"
            )
        head = f"触发规则已重新加载，生效 {count} 条。"
        if self.__skipped > 0:
            head += f"（其中 {self.__skipped} 条正则非法被跳过，详见日志）"
        return f"{head}\n\n{self.describe_triggers()}"

    async def match(self, message: _t.Any) -> tuple[bool, str]:
        """群 text / interactive 消息正文匹配触发规则。

        返回 ``(matched, context)``：第一条 ``pattern.search`` 命中且
        群 chat_id 在规则 ``chat_ids`` 白名单内（或规则不限群）的
        context（未配置则为空串）；未命中 / 非群 / 正文解析失败均
        返回 ``(False, "")``。

        text 消息取 content 的 ``text`` 字段；interactive 卡片经
        ``interactive_card_to_markdown`` 渲染成 Markdown 后匹配
        —— 与发给 AI 的正文同源（见 ``_parse_message_content`` 覆写），
        注意 ``^`` 等锚定正则针对渲染文本：卡片标题渲染为 ``# 标题``，
        锚定标题需写成 ``^#``。其余类型（post / 媒体消息等）结构
        各异，不猜、不匹配。
        """
        if not self.__rules:
            return False, ''

        chat_type    = str(getattr(message, "chat_type", "p2p") or "p2p").strip()
        message_type = str(getattr(message, "message_type", "") or "").strip()
        chat_id      = str(getattr(message, "chat_id", "") or "").strip()
        content_raw  = getattr(message, "content", None) or ""
        if chat_type != "group":
            return False, ""
        if message_type == "text":
            try:
                text = str(
                    (json.loads(content_raw) or {}).get("text", "") or "",
                )
            except (ValueError, TypeError):
                return False, ""
        elif message_type == "interactive":
            text = await interactive_card_to_markdown(content_raw) or ""
            if not text:
                return False, ""
        else:
            return False, ""
        for rule in self.__rules:
            # 规则限定群且当前群不在白名单 → 跳过该规则，继续找下一条。
            if rule.chat_ids and chat_id not in rule.chat_ids:
                continue
            if rule.pattern.search(text):
                return True, rule.context
        return False, ""
