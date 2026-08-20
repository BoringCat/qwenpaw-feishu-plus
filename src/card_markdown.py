# -*- coding: utf-8 -*-
"""interactive 卡片 → Markdown 渲染器（纯逻辑，无 SDK / 网络依赖）。

内置 FeishuChannel 的 ``extract_interactive_text`` 把卡片压成单行，且
CardKit v2 ``div`` 的正文在 ``text.content`` 键 —— 不在递归的
_CHILD_KEYS 里 —— 整体丢失。本模块按卡片结构渲染成 Markdown：

* ``header.title`` → ``# 标题``；
* ``div`` / ``markdown`` → lark_md 原文（内联标签清洗后）保留，
  卡片内的 Markdown 表格原样可用；
* 原生 ``table`` 组件 → GFM 表格；
* ``hr`` / ``button`` 忽略（对模型无信息量，详情按钮不渲染）；
* ``column_set`` / ``column`` / 未知容器 tag → 递归子元素；
* ``note``（灰色小字备注）→ ``> `` markdown 引用块；
* 内联清洗：``<text_tag>`` 去壳、``<at id=..>`` → @名字、
  ``<img>`` → [图片]；emoji 短代码（:DONE: 等）保留原样。

``at_resolver`` 由调用方注入（FeishuChannel._get_user_name_by_open_id，
带缓存）；解析失败回退 ``@id后4位``。渲染失败返回 None，
调用方回退父类单行压平逻辑。
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

AtResolver = Callable[[str], Awaitable[Optional[str]]]

# 收集 / 替换 <at> 标签（id 引号可选，闭合标签内可内嵌名字）。
_AT_ID_RE = re.compile(r"<at\s+id=[\"']?([A-Za-z0-9_]+)[\"']?")
_AT_FULL_RE = re.compile(
    r"<at\s+id=([\"']?)([A-Za-z0-9_]+)\1\s*>([^<]*)</at>",
)
# 内联富文本标签。
_TEXT_TAG_RE = re.compile(r"<text_tag[^>]*>(.*?)</text_tag>", re.S)
_IMG_RE = re.compile(r"<img\b[^>]*>")

# text.content 形态的正文 tag（v2 markdown / v1 lark_md 直出 / 注脚）。
_TEXT_LIKE_TAGS = frozenset(
    {"markdown", "md", "lark_md", "plain_text", "text", "code_block"},
)

# 可能嵌套子元素的容器键。
_CHILD_KEYS = ("elements", "columns", "body", "content", "actions")

AtNames = Dict[str, Optional[str]]


# ====================================================================
# 内联清洗
# ====================================================================


def quote_lines(text: str) -> str:
    """每行加 ``> `` 前缀（空行变 ``>``），用于 note / 引用块。"""
    return "\n".join(
        f"> {line}".rstrip() if line.strip() else ">"
        for line in text.splitlines()
    )


def _clean_inline(text: str, at_names: AtNames) -> str:
    """清洗 lark_md 内联标签为纯 Markdown。"""
    # <text_tag ...>y</text_tag> → y（相邻标签去壳后粘连，补尾随空格）。
    text = _TEXT_TAG_RE.sub(lambda m: (m.group(1) or "").strip() + " ", text)
    # <at id=ou_x>名字</at> / <at id=ou_x></at> → @名字。
    def _at_repl(m: "re.Match[str]") -> str:
        open_id = m.group(2)
        inner = (m.group(3) or "").strip()
        if inner:
            return f"@{inner}"
        name = at_names.get(open_id)
        if name:
            return f"@{name}"
        return f"@{open_id[-4:]}" if len(open_id) >= 4 else f"@{open_id}"

    text = _AT_FULL_RE.sub(_at_repl, text)
    # <img key=...> → [图片]。
    text = _IMG_RE.sub("[图片]", text)
    return text


# ====================================================================
# 结构渲染
# ====================================================================


def _render_table(item: Dict[str, Any]) -> Optional[str]:
    """原生 table 组件 → GFM 表格（无表头且无数据时返回 None）。"""
    names: List[str] = []
    headers: List[str] = []
    for col in item.get("columns") or []:
        if not isinstance(col, dict):
            continue
        names.append(str(col.get("name", "") or ""))
        headers.append(
            str(col.get("display_name", "") or col.get("name", "") or ""),
        )
    if not headers:
        return None
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    has_row = False
    for row in item.get("rows") or []:
        if not isinstance(row, dict):
            continue
        cells = [str(row.get(n, "") or "") for n in names]
        lines.append("| " + " | ".join(cells) + " |")
        has_row = True
    return "\n".join(lines) if has_row else None


def _render_div(item: Dict[str, Any], at_names: AtNames) -> Optional[str]:
    """div 元素 —— 正文在 text.content（v2 卡片的主体内容位置）。"""
    text = item.get("text")
    if isinstance(text, dict):
        content = text.get("content")
        if isinstance(content, str) and content.strip():
            return _clean_inline(content, at_names)
        # text 无 content（img / 组件等），递归其子元素。
        children = _render_children(text, at_names)
        return "\n\n".join(children) if children else None
    if isinstance(text, str) and text.strip():
        return _clean_inline(text, at_names)
    return None


def _render_note(item: Dict[str, Any], at_names: AtNames) -> Optional[str]:
    """note 元素（灰色小字备注）→ ``> `` markdown 引用块。"""
    texts = _render_children(item, at_names)
    if not texts:
        return None
    return quote_lines("\n\n".join(texts))


def _render_children(item: Dict[str, Any], at_names: AtNames) -> List[str]:
    """递归渲染容器键（elements/columns/body/content/actions）中的 list。"""
    blocks: List[str] = []
    for key in _CHILD_KEYS:
        children = item.get(key)
        if isinstance(children, list):
            blocks.extend(_render_elements(children, at_names))
    return blocks


def _render_elements(
    elements: List[Any],
    at_names: AtNames,
) -> List[str]:
    """逐元素渲染为 Markdown 块列表。"""
    blocks: List[str] = []
    for item in elements:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag", "") or "")
        # hr / button 忽略：对模型无信息量。
        if tag in ("hr", "button"):
            continue
        if tag == "div":
            block = _render_div(item, at_names)
        elif tag == "table":
            block = _render_table(item)
        elif tag == "note":
            block = _render_note(item, at_names)
        elif tag in _TEXT_LIKE_TAGS:
            content = item.get("content") or item.get("text") or ""
            block = None
            if isinstance(content, dict):
                content = content.get("content") or content.get("text") or ""
            if isinstance(content, str) and content.strip():
                block = _clean_inline(content, at_names)
        else:
            # column_set / column / note / 未知容器 → 递归子元素。
            blocks.extend(_render_children(item, at_names))
            continue
        if block and block.strip():
            blocks.append(block.strip())
    return blocks


# ====================================================================
# 入口
# ====================================================================


async def interactive_card_to_markdown(
    content: Optional[str],
    at_resolver: Optional[AtResolver] = None,
) -> Optional[str]:
    """interactive 卡片 content JSON → Markdown 文本。

    Returns:
        渲染后的 Markdown（块间空行分隔）；content 非法或渲染为空时
        返回 None，调用方回退父类单行压平逻辑。
    """
    if not content:
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    # 预解析卡片内全部 <at> 的 open_id（resolver 失败记 None，
    # 清洗时回退 @id后4位）。
    at_names: AtNames = {}
    for open_id in _AT_ID_RE.findall(content):
        if open_id in at_names:
            continue
        name: Optional[str] = None
        if at_resolver is not None:
            try:
                name = await at_resolver(open_id)
            except Exception:  # noqa: BLE001 - resolver 异常不阻断渲染
                name = None
        at_names[open_id] = name if isinstance(name, str) else None

    blocks: List[str] = []

    # 标题：v2 header.title 优先，v1 卡片顶层 title 兜底。
    title: Any = None
    header = data.get("header")
    if isinstance(header, dict):
        title = header.get("title")
    if title is None:
        title = data.get("title")
    if isinstance(title, dict):
        title = title.get("content")
    if isinstance(title, str) and title.strip():
        blocks.append(f"# {_clean_inline(title.strip(), at_names)}")

    # 元素树：v1 顶层 elements 优先，v2 schema 2.0 在 body.elements。
    elements = data.get("elements")
    if not elements:
        body = data.get("body")
        if isinstance(body, dict):
            elements = body.get("elements")
    if isinstance(elements, list):
        blocks.extend(_render_elements(elements, at_names))

    cleaned = [b.strip() for b in blocks if b and b.strip()]
    return "\n\n".join(cleaned) if cleaned else None
