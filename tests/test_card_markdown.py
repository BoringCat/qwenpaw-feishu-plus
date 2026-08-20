# -*- coding: utf-8 -*-
"""card_markdown 渲染器测试。

真实告警卡片含内网数据不入库；此处用结构同款的合成脱敏卡片覆盖
（div/lark_md、text_tag、markdown 表格、原生 table、hr、button、
column_set、at、emoji 短代码）。若 ``test_data/zabbix_card.json``
存在（用户自行放入的脱敏版），则额外用真实结构跑一遍冒烟断言。
"""
import json
from pathlib import Path
from typing import Dict, Optional

import pytest

from src.card_markdown import interactive_card_to_markdown

TEST_DATA = Path(__file__).parent / "test_data"

# 假 open_id（结构同真实 ou_xxx，无敏感数据）。
_AT_ID_1 = "ou_aaa0123456789abcdef0123456789aaa"
_AT_ID_2 = "ou_bbb0123456789abcdef0123456789bbb"
_AT_NAMES: Dict[str, Optional[str]] = {_AT_ID_1: "张三", _AT_ID_2: "李四"}


async def _fake_resolver(open_id: str) -> Optional[str]:
    return _AT_NAMES.get(open_id)


def _synth_card() -> Dict:
    """结构同 FlashDuty 告警卡的合成卡片（脱敏）。"""
    return {
        "schema": "2.0",
        "config": {"streaming_mode": False},
        "header": {
            "template": "green",
            "title": {
                "content": "【已关闭】#AAA001 Nginx每分钟服务端错误数量(5XX) > 300",
                "tag": "plain_text",
            },
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "content": (
                            "<text_tag color='turquoise'>component=nginx"
                            "</text_tag><text_tag color='turquoise'>"
                            "domain=example.test</text_tag>\n\n"
                            ":DONE: **已恢复**\n"
                            "| 服务实例 | 服务 | 时间(UTC+8) | 链接 | 触发时指标 |\n"
                            "| -------- | -------- | -------- | -------- | -------- |\n"
                            "| web_10.0.0.1 | <no value> | 2026-08-20 04:56:00 | "
                            "[detail_url](https://zabbix.example.test/tr_events.php"
                            "?triggerid=1&eventid=2) | example.test('10.0.0.1:80'): "
                            "每分钟服务端错误数量(Nginx 5XX): 8964 |"
                        ),
                        "tag": "lark_md",
                    },
                },
                {
                    "tag": "table",
                    "columns": [
                        {"name": "column_1", "display_name": "触发时间", "data_type": "text"},
                        {"name": "column_2", "display_name": "来源", "data_type": "text"},
                        {"name": "column_3", "display_name": "项目-分区", "data_type": "text"},
                        {"name": "column_4", "display_name": "故障ID", "data_type": "text"},
                    ],
                    "rows": [
                        {
                            "column_1": "2026-08-20 04:38:54",
                            "column_2": "Zabbix",
                            "column_3": "pja-demo",
                            "column_4": "6aaa14a0eb4d195055e4a0aa",
                        },
                    ],
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "content": (
                            "🧩 **协作空间:** demo\n"
                            "🔥 **严重程度:** **Critical (自动恢复，持续17m26s)**\n"
                            "🕵️ **处理人员:** "
                            f"<at id={_AT_ID_1}></at> <at id={_AT_ID_2}></at>"
                        ),
                        "tag": "lark_md",
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "content": "故障 **自动恢复**，持续 17m26s | 08-20 04:56",
                        "tag": "lark_md",
                    },
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"content": "🔎 详情", "tag": "plain_text"},
                                    "url": "https://applink.example.test/detail/1",
                                },
                            ],
                        },
                    ],
                },
            ],
        },
    }


def _dump(card: Dict) -> str:
    return json.dumps(card, ensure_ascii=False)


# ====================================================================
# 合成卡片 —— 完整渲染
# ====================================================================


@pytest.mark.asyncio
async def test_synth_card_full_render() -> None:
    md = await interactive_card_to_markdown(
        _dump(_synth_card()),
        at_resolver=_fake_resolver,
    )
    assert md is not None

    lines = md.splitlines()
    # 标题渲染为 # 标题（第一块）。
    assert lines[0] == (
        "# 【已关闭】#AAA001 Nginx每分钟服务端错误数量(5XX) > 300"
    )
    # div 正文完整保留（父类实现会整体丢失 div）。
    assert "component=nginx domain=example.test" in md
    assert ":DONE: **已恢复**" in md  # emoji 短代码保留原样
    assert (
        "| web_10.0.0.1 | <no value> | 2026-08-20 04:56:00 | "
        "[detail_url](https://zabbix.example.test/tr_events.php"
        "?triggerid=1&eventid=2) | example.test('10.0.0.1:80'): "
        "每分钟服务端错误数量(Nginx 5XX): 8964 |" in md
    )
    # 原生 table → GFM 表格。
    assert "| 触发时间 | 来源 | 项目-分区 | 故障ID |" in md
    assert "| --- | --- | --- | --- |" in md
    assert (
        "| 2026-08-20 04:38:54 | Zabbix | pja-demo | 6aaa14a0eb4d195055e4a0aa |"
        in md
    )
    # <at id=..></at> → @名字（resolver 命中）。
    assert "🕵️ **处理人员:** @张三 @李四" in md
    assert "故障 **自动恢复**，持续 17m26s | 08-20 04:56" in md
    # hr / button 忽略。
    assert "🔎 详情" not in md
    assert "applink.example.test" not in md
    assert "---" not in [ln.strip() for ln in lines]


# ====================================================================
# 边界与内联清洗
# ====================================================================


@pytest.mark.asyncio
async def test_at_resolver_failure_falls_back_to_id_suffix() -> None:
    async def _fail(open_id: str) -> Optional[str]:
        return None

    card = {
        "header": {"title": {"content": "t"}},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "content": f"<at id={_AT_ID_1}></at>",
                    "tag": "lark_md",
                },
            },
        ],
    }
    md = await interactive_card_to_markdown(
        _dump(card),
        at_resolver=_fail,
    )
    # 解析失败回退 @id后4位。
    assert md == f"# t\n\n@{_AT_ID_1[-4:]}"


@pytest.mark.asyncio
async def test_at_inline_name_wins() -> None:
    card = {
        "header": {"title": {"content": "t"}},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "content": f"<at id={_AT_ID_1}>内嵌名</at>",
                    "tag": "lark_md",
                },
            },
        ],
    }
    md = await interactive_card_to_markdown(_dump(card))
    assert "@内嵌名" in md


@pytest.mark.asyncio
async def test_v1_card_top_level_elements() -> None:
    """v1 卡片：顶层 elements + 顶层 title dict。note → 引用块。"""
    card = {
        "title": {"content": "v1标题", "tag": "plain_text"},
        "elements": [
            {"tag": "markdown", "content": "v1 正文"},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "注脚内容"},
                ],
            },
        ],
    }
    md = await interactive_card_to_markdown(_dump(card))
    assert md == "# v1标题\n\nv1 正文\n\n> 注脚内容"


@pytest.mark.asyncio
async def test_v1_logstash_card() -> None:
    """v1 Logstash 告警卡（结构同真实卡片，字段值脱敏）。"""
    card = {
        "config": {},
        "header": {
            "template": "green",
            "title": {
                "content": "[OK] Logstash 10分钟内有错误日志大于1条",
                "i18n": {},
                "tag": "plain_text",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "text_align": "left",
                "content": (
                    "**项目:** demo-log\n"
                    "**实例:** ls-cn-demo00\n"
                    "**触发时间:** 2026-08-20T16:59:50+08:00\n"
                    "**恢复时间:** 2026-08-20T17:08:20+08:00"
                ),
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "lark_md",
                        "content": (
                            "- 对象 is logstash.outputs.elasticsearch\n"
                            "- 日志数量 is 1\n"
                            "- 日志等级 is WARN\n"
                        ),
                    },
                ],
            },
            {
                "tag": "markdown",
                "text_align": "left",
                "content": (
                    "**持续时间:** 8分30秒\n"
                    "**分析时间:** 2026-08-20T17:10:45.608326597+08:00"
                ),
            },
        ],
    }
    md = await interactive_card_to_markdown(_dump(card))
    assert md == "\n\n".join(
        [
            "# [OK] Logstash 10分钟内有错误日志大于1条",
            "**项目:** demo-log\n"
            "**实例:** ls-cn-demo00\n"
            "**触发时间:** 2026-08-20T16:59:50+08:00\n"
            "**恢复时间:** 2026-08-20T17:08:20+08:00",
            "> - 对象 is logstash.outputs.elasticsearch\n"
            "> - 日志数量 is 1\n"
            "> - 日志等级 is WARN",
            "**持续时间:** 8分30秒\n"
            "**分析时间:** 2026-08-20T17:10:45.608326597+08:00",
        ],
    )


@pytest.mark.asyncio
async def test_inline_img_and_text_tag() -> None:
    card = {
        "elements": [
            {
                "tag": "div",
                "text": {
                    "content": (
                        "<text_tag color='red'>label-a</text_tag>"
                        "<text_tag color='blue'>label-b</text_tag>"
                        " <img key='img_v2_demo'/>"
                    ),
                    "tag": "lark_md",
                },
            },
        ],
    }
    md = await interactive_card_to_markdown(_dump(card))
    # 相邻 text_tag 去壳后补尾随空格防粘连；img → [图片]。
    assert md == "label-a label-b  [图片]"


@pytest.mark.asyncio
async def test_div_without_text_skipped() -> None:
    card = {"elements": [{"tag": "div"}]}
    assert await interactive_card_to_markdown(_dump(card)) is None


@pytest.mark.asyncio
async def test_empty_table_skipped() -> None:
    card = {
        "elements": [{"tag": "table", "columns": [{"name": "c1"}]}],
    }
    assert await interactive_card_to_markdown(_dump(card)) is None


@pytest.mark.parametrize(
    "bad",
    ["", None, "not json", "[1, 2]", '{"header": {}}'],
)
@pytest.mark.asyncio
async def test_invalid_content_returns_none(bad) -> None:
    assert await interactive_card_to_markdown(bad) is None


# ====================================================================
# 可选：用户自行放入的脱敏真实卡片（test_data/zabbix_card.json）
# ====================================================================

_REAL_CARD = TEST_DATA / "zabbix_card.json"


@pytest.mark.skipif(
    not _REAL_CARD.exists(),
    reason="test_data/zabbix_card.json 未提供（含内网数据不入库，可自行放入脱敏版）",
)
@pytest.mark.asyncio
async def test_real_card_smoke() -> None:
    content = _REAL_CARD.read_text(encoding="UTF-8")
    md = await interactive_card_to_markdown(
        content,
        at_resolver=_fake_resolver,
    )
    assert md is not None
    assert md.startswith("# ")
    assert "| 服务实例 | 服务 |" in md
    assert "**已恢复**" in md
