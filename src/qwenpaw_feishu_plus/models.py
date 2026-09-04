
# ── 触发规则 YAML 的 pydantic 模型 ──
#
# ``Trigger.load`` 先用 ``yaml.safe_load`` 读文件，再用
# ``TriggerRulesFile`` 校验结构。结构 / 类型错误（多余字段、非字符串
# pattern、非列表 triggers 等）会整份拒绝加载 —— 配置错误应显式暴露，
# 而不是静默丢弃某条规则；正则本身编译失败仍逐条跳过（字符串在
# 结构上合法，只是正则非法），见 ``Trigger.load``。

import re
import typing as _t

from pydantic import BaseModel, ConfigDict, field_validator

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

