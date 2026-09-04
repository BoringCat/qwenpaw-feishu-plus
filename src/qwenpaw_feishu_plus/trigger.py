# -*- coding: utf-8 -*-

# ── 触发规则 YAML 的 pydantic 模型 ──
#
# ``Trigger.load`` 先用 ``yaml.safe_load`` 读文件，再用
# ``TriggerRulesFile`` 校验结构。结构 / 类型错误（多余字段、
# ``TriggerMatch`` 的 ``regex`` / ``keyword`` 不是恰好一个非空、
# 非列表 triggers 等）会整份拒绝加载 —— 配置错误应显式暴露，
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
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from .card.markdown import interactive_card_to_markdown

# 触发规则 YAML 的默认文件名（位于 workspace 根目录下）。
TRIGGER_YAML_DEFAULT_NAME = "feishu_plus_triggers.yaml"

logger = logging.getLogger(__name__)

class TriggerMatch(BaseModel):
    """单个匹配条件：``regex`` / ``keyword`` 恰好提供一个。"""

    model_config = ConfigDict(extra="forbid")

    regex:   str = ""
    '正则（``re.search`` 语义）；与 keyword 二选一。'
    keyword: str = ""
    '字面关键词（子串匹配，正则元字符不生效）；与 regex 二选一。'

    @field_validator('regex', 'keyword', mode="before")
    @classmethod
    def _normalize_pattern(cls, value: _t.Any) -> _t.Any:
        """去首尾空白；``null`` 视作空串（未提供该匹配方式）。"""
        if value is None:
            return ""
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _exactly_one(self) -> "TriggerMatch":
        """regex 与 keyword 恰好一个非空（都缺 / 都给 → 结构错误）。"""
        if bool(self.regex) == bool(self.keyword):
            raise ValueError("exactly one of regex / keyword is required")
        return self

class TriggerRule(BaseModel):
    """单条触发规则"""

    model_config = ConfigDict(extra="forbid")

    chat_ids: list[str] = []
    '限定规则生效的群（chat_id 白名单）；空 = 全部群生效。'

    must: list[TriggerMatch]
    '必须满足的条件'
    must_not: list[TriggerMatch] = []
    '必须排除的条件'
    should: list[TriggerMatch] = []
    '尽力满足的条件'
    minimum_should_match: int = 1
    'should 最小满足个数'

    context: str = ""
    '追加到上下文的提示词'

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

    @model_validator(mode="after")
    def _require_positive_condition(self) -> "TriggerRule":
        """must / should 至少一个非空 —— 全空（或仅 must_not）的条件组
        会命中所有（不含排除词的）消息，必是配置错误，显式报错。"""
        if not (self.must or self.should):
            raise ValueError(
                "at least one of must / should is required "
                "(must_not-only would match everything)",
            )
        return self

class TriggerRulesFile(BaseModel):
    """触发规则 YAML 顶层结构：``triggers`` 规则列表。"""

    model_config = ConfigDict(extra="forbid")

    triggers: list[TriggerRule]

class CompiledMatch(_t.NamedTuple):
    """编译好的单个匹配条件：``regex`` / ``keyword`` 二选一。

    ``CompiledTrigger`` 各条件组的元素类型；``hit`` 统一两种匹配
    语义 —— 正则 ``re.search`` 命中，关键词为子串包含（字面匹配，
    正则元字符不生效）。
    """

    regex:   re.Pattern|None = None
    keyword: str             = ""

    def hit(self, text: str) -> bool:
        return bool(self.regex.search(text)) if self.regex else self.keyword in text

class CompiledTrigger(_t.NamedTuple):
    """编译好的单条规则：bool 条件组 + context + chat_ids（空 = 全部群）。

    ``Trigger.rules`` 的元素类型。命中语义（``Trigger.match``）：
    ``must`` 全部 hit、``must_not`` 全部不 hit、``should`` 至少
    ``minimum_should_match`` 个 hit（``should`` 为空时无该约束）。
    """

    must:      tuple[CompiledMatch, ...] = ()
    must_not:  tuple[CompiledMatch, ...] = ()
    should:    tuple[CompiledMatch, ...] = ()
    minimum_should_match: int = 1
    context:   str = ""
    chat_ids:  tuple[str, ...] = ()

def _compile_rule(idx: int, rule: TriggerRule) -> CompiledTrigger|None:
    """把校验过的规则编译成 ``CompiledTrigger``；任一正则非法返回 None。

    正则编译失败只跳过本条规则（字符串在结构上合法，只是正则
    非法），不影响其余条目 —— 见 ``Trigger.load``。
    """
    groups: dict[str, tuple[CompiledMatch, ...]] = {}
    for section in ("must", "must_not", "should"):
        compiled = []
        for m in getattr(rule, section):
            try:
                compiled.append(CompiledMatch(
                    regex   = re.compile(m.regex) if m.regex else None,
                    keyword = m.keyword,
                ))
            except re.error as exc:
                logger.warning(
                    "feishu-plus trigger yaml: rule #%d invalid regex in "
                    "%s %r (%s), rule skipped",
                    idx,
                    section,
                    m.regex,
                    exc,
                )
                return None
        groups[section] = tuple(compiled)
    return CompiledTrigger(
        must      = groups["must"],
        must_not  = groups["must_not"],
        should    = groups["should"],
        minimum_should_match = rule.minimum_should_match,
        context   = rule.context,
        chat_ids  = tuple(rule.chat_ids),
    )

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
        # ── 触发规则（from_config 覆盖；见 Trigger.load） ──
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
    # 触发规则 —— YAML 加载 / 匹配 / mention 绕过 / 自动进话题
    # ------------------------------------------------------------------

    def load(self) -> tuple[bool, int]:
        """加载触发规则 YAML（顶层 ``triggers:`` 列表）。

        文件结构由 ``TriggerRulesFile`` / ``TriggerRule`` /
        ``TriggerMatch`` 三个 pydantic 模型校验：``triggers`` 为规则
        列表，每条 ``must``（必填）/ ``must_not`` / ``should`` 为匹配
        条件组，每条条件 ``regex``（``re.search`` 语义）或 ``keyword``
        （字面子串）恰好提供一个；``minimum_should_match`` /
        ``context`` / ``chat_ids`` 可选（末者为群 chat_id 白名单，
        空 = 全部群生效），多余字段与类型错误整份置空。正则编译
        失败逐条跳过并告警，不影响其余条目。文件不存在 / 解析失败 /
        结构不符只记日志并置空规则，不抛异常 —— 触发规则是增强
        能力，不应因配置问题阻断渠道启动。

        调用副作用（供 ``/feishu-plus`` 命令反馈）：失败时置
        ``self.__load_error`` 为人类可读原因（成功 / 默认文件缺失
        则保持空串），被跳过的含非法正则规则条数写入 ``self.__skipped``。
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
            compiled = _compile_rule(idx, rule)
            if compiled is None:
                skipped += 1
                continue
            rules.append(compiled)
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

        列出规则文件路径、相关触发开关与逐条生效规则（must /
        must_not / should 条件组 / context / chat_ids 白名单）。规则
        为空时给出原因（文件缺失 / 加载失败 / 文件中没有规则），
        便于运维定位。
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
            # 编号只落在首个非空条件组上；后续组与属性行缩进对齐。
            head = f"{idx}."
            for section, label in (
                ("must", "must（须全部命中）"),
                ("must_not", "must_not（须全部不命中）"),
                (
                    "should",
                    f"should（至少命中 {rule.minimum_should_match} 个）",
                ),
            ):
                matches = getattr(rule, section)
                if not matches:
                    continue
                lines.append(f"{head} {label}")
                head = "  "
                for m in matches:
                    if m.regex:
                        lines.append(f"     - regex: `{m.regex.pattern}`")
                    else:
                        lines.append(f"     - keyword: `{m.keyword}`")
            if rule.context:
                lines.append(f"   context: {rule.context}")
            lines.append(f"   生效范围: {scope}")
        if self.__skipped:
            lines.append(
                ""
                f"（本次加载 {self.__skipped} 条规则因正则非法被跳过）",
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
            head += f"（其中 {self.__skipped} 条规则因正则非法被跳过，详见日志）"
        return f"{head}\n\n{self.describe_triggers()}"

    async def match(self, message: _t.Any) -> tuple[bool, str]:
        """群 text / interactive 消息正文匹配触发规则。

        返回 ``(matched, context)``：第一条条件组满足（``must`` 全部
        命中、``must_not`` 全部不命中、``should`` 至少
        ``minimum_should_match`` 个命中）且群 chat_id 在规则
        ``chat_ids`` 白名单内（或规则不限群）的 context（未配置则为
        空串）；未命中 / 非群 / 正文解析失败均返回 ``(False, "")``。

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
            if not all(m.hit(text) for m in rule.must):
                continue
            if any(m.hit(text) for m in rule.must_not):
                continue
            if rule.should and sum(
                m.hit(text) for m in rule.should
            ) < rule.minimum_should_match:
                continue
            return True, rule.context
        return False, ""
