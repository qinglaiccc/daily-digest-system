# -*- coding: utf-8 -*-
"""
Obsidian 知识库导出层。

职责：把同一份 report 数据额外渲染成 obsidian/YYYY-MM-DD.md，
带 YAML 前置区（frontmatter）、五大板块标题、实体双链与待办清单。

为什么单独一个模块而不是塞进 renderer.py：
    renderer.py 负责的是"发到公网给人看"的 HTML，本模块负责的是"进私人知识库"，
    两者的文本处理规则刚好相反 —— 网页/邮件必须把 [[双链]] 还原成纯文本（见
    strip_wikilinks），而 Obsidian 必须原样保留。规则分开放，改一边不会误伤另一边。

依赖方向：本模块只依赖 config，不 import renderer / summarizer / notifier，
所以 renderer、notifier、main 都可以安全地反过来 import 本模块，不会成环。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import config

logger = logging.getLogger(__name__)

# [[目标]] 或 [[目标|显示文字]]。
# 字符类里排掉方括号，是为了让形如 [[a[[b]]]] 的畸形输入不会整段被吞掉。
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]+))?\]\]")

# 只认这种日期做文件名。这一步是安全边界：report["date"] 理论上来自模型/存档，
# 万一被污染成 "../x" 之类的值，没有校验就会把文件写到 obsidian/ 外面去。
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Obsidian 标签的合法字符只有字母、数字、下划线、连字符（以及用于嵌套标签的 /）。
# 这里把其余全部归一到连字符：空格、YAML 里要转义的字符（: # [ ] { } , & * ! | > % @ ` 引号）、
# 以及各种标点。归一到连字符而不是直接删掉，是为了保留 "machine learning" → "machine-learning"
# 这种可读性；如果直接删会变成 "machinelearning"。
_TAG_BAD_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff\u3400-\u4dbf_-]+")


# --------------------------------------------------------------------------
# 双链处理
# --------------------------------------------------------------------------
def _unwrap(text: str) -> str:
    """把 [[目标|显示文字]] 换成"显示文字"，[[目标]] 换成"目标"。"""
    return _WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), text)


def strip_wikilinks(text: Optional[str]) -> str:
    """
    把双链降级成纯文本，供网页 / 邮件 / history.json 使用。

    [[Anthropic]] 只对 Obsidian 有意义，直接输出到网页上就是一对多余的方括号，
    所以非 Obsidian 通道一律走这个函数还原成 Anthropic。

    这里会把残留的孤立 [[ 与 ]] 一并清掉。这么处理是安全的：网页与邮件是 HTML，
    不存在 Markdown 的 [文字](链接) 语法，不会有 "合法但长得像 ]]" 的结构被误伤。
    孤立括号的来源很实际 —— clean_text 按字符数截断摘要时，会把一个双链拦腰切断，
    留下 "[[Anthrop…" 这种东西。
    """
    if not text:
        return text or ""
    if "[[" not in text and "]]" not in text:
        return text

    text = _unwrap(text)
    return text.replace("[[", "").replace("]]", "")


def repair_wikilinks(text: Optional[str]) -> str:
    """
    Obsidian 专用：保留合法双链，只清掉没有闭合的孤立 [[。

    与 strip_wikilinks 的关键区别是**不能**无脑删 "]]"：Markdown 里
    [见 [1]](url) 这种写法本身就含 "]]"，删了会把链接弄坏。
    所以这里只处理"有 [[ 却找不到 ]]"这一种情况，其余原样保留。
    """
    if not text:
        return text or ""
    if "[[" not in text:
        return text

    out: List[str] = []
    pos = 0
    while True:
        start = text.find("[[", pos)
        if start == -1:
            out.append(text[pos:])
            break

        end = text.find("]]", start + 2)
        if end == -1:
            # 这个 [[ 永远闭不上了（截断的典型症状），丢掉括号本身但保留后面的文字
            out.append(text[pos:start])
            out.append(text[start + 2 :])
            break

        out.append(text[pos : end + 2])
        pos = end + 2

    return "".join(out)


def extract_wikilinks(text: Optional[str]) -> List[str]:
    """取出文本里所有双链的目标名（[[A|B]] 取 A）。"""
    if not text:
        return []
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(text)]


def strip_wikilinks_deep(value: Any) -> Any:
    """
    递归地把整个结构里所有字符串的双链拆掉。

    邮件那边用它一次处理整份 report（主题、纯文本正文、HTML 正文都要用干净数据），
    比在十几处输出点分别调用可靠。
    """
    if isinstance(value, str):
        return strip_wikilinks(value)
    if isinstance(value, dict):
        return {k: strip_wikilinks_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [strip_wikilinks_deep(v) for v in value]
    return value


# --------------------------------------------------------------------------
# 标签
# --------------------------------------------------------------------------
def sanitize_tag(raw: Any, limit: int = 30) -> str:
    """
    把任意字符串消毒成一个合法的 Obsidian 标签，不合法就返回空串。

    要同时满足两边的约束：
      · YAML —— 标签出现在 frontmatter 里，含 : # [ ] { } , & * 等字符会直接把
        frontmatter 解析搞崩，导致整篇笔记的属性全部丢失。
      · Obsidian —— 标签只能由字母、数字、_、-、/ 组成，不能含空格，也不能是纯数字。
    """
    if raw is None:
        return ""
    text = _unwrap(str(raw)).strip()
    if not text:
        return ""

    text = _TAG_BAD_RE.sub("-", text)
    text = text.strip("-_. ")

    if len(text) > limit:
        text = text[:limit].strip("-_. ")

    # 纯数字标签会被 Obsidian 拒绝，直接丢掉
    if not text or text.isdigit():
        return ""

    return text


def coerce_keywords(value: Any, limit: Optional[int] = None) -> List[str]:
    """
    规整模型返回的 keywords：既接受数组，也接受"用逗号/顿号分隔的一整段文字"。
    """
    cap = limit if limit is not None else config.OBSIDIAN_MAX_KEYWORDS * 2

    if isinstance(value, str):
        parts: List[Any] = re.split(r"[,，、;；|\n]+", value)
    elif isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.extend(re.split(r"[,，、;；|\n]+", item))
            else:
                parts.append(item)
    else:
        return []

    result: List[str] = []
    seen = set()
    for part in parts:
        tag = sanitize_tag(part)
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= cap:
            break
    return result


def normalize_tags(keywords: Optional[Iterable[Any]] = None) -> List[str]:
    """固定标签 + 模型关键词，去重后返回最终的 tags 列表。"""
    tags: List[str] = []
    seen = set()

    def _push(raw: Any) -> bool:
        tag = sanitize_tag(raw)
        if not tag:
            return False
        key = tag.casefold()
        if key in seen:
            return False
        seen.add(key)
        tags.append(tag)
        return True

    for fixed in config.OBSIDIAN_FIXED_TAGS:
        _push(fixed)

    added = 0
    for kw in keywords or []:
        if added >= config.OBSIDIAN_MAX_KEYWORDS:
            break
        if _push(kw):
            added += 1

    return tags


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def markdown_path(for_date: str) -> Path:
    if not _DATE_RE.fullmatch(for_date or ""):
        raise ValueError(f"非法的日期，拒绝生成 Obsidian 文件名：{for_date!r}")
    return config.OBSIDIAN_DIR / f"{for_date}.md"


def _format_stars(value: Any) -> str:
    """把星数格式化成 1.2k 这种易读形式（与网页端口径保持一致）。"""
    try:
        num = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return str(value or "")
    if num >= 1000:
        return f"{num / 1000:.1f}k".replace(".0k", "k")
    return str(num)


def _counts(report: Dict[str, Any]) -> Dict[str, int]:
    sections = report.get("sections") or []
    items = sum(len(s.get("items") or []) for s in sections)
    repos = sum(
        len(s.get("items") or []) for s in sections if s.get("key") == "github"
    )
    sources = {
        item.get("source")
        for s in sections
        if s.get("key") != "github"
        for item in (s.get("items") or [])
        if item.get("source")
    }
    return {
        "items": items,
        "news": items - repos,
        "repos": repos,
        "sources": len(sources),
    }


def build_frontmatter(report: Dict[str, Any], tags: List[str]) -> List[str]:
    """
    生成 YAML 前置区。

    title / date / tags 是要求里点名必须有的；type 与四个计数是为了 Dataview 之类的
    数据库插件好用（例如 TABLE items FROM "obsidian" WHERE type = "早报"）。
    不需要的话删掉那几行即可，不影响正文。
    """
    date_str = report["date"]
    counts = _counts(report)

    lines = [
        "---",
        f"title: 每日早报-{date_str}",
        f"date: {date_str}",
        "type: 早报",
        "tags:",
    ]
    for tag in tags:
        lines.append(f"  - {tag}")

    lines += [
        f"items: {counts['items']}",
        f"news: {counts['news']}",
        f"repos: {counts['repos']}",
        f"sources: {counts['sources']}",
        "---",
    ]
    return lines


def _render_news_item(item: Dict[str, Any]) -> List[str]:
    """一条新闻：加粗标题 + 带双链的正文 + 来源超链接。"""
    title = repair_wikilinks(item.get("title", ""))
    summary = repair_wikilinks(item.get("summary", ""))
    url = item.get("url", "")
    source = item.get("source") or "原文"

    out = [f"**{title}**", ""]

    body = summary or "（本条无摘要）"
    # 来源链接跟在正文末尾，而不是单独占一行：一条新闻在 Obsidian 里就是一个自然段，
    # 阅读视图下更紧凑，也便于整段复制到别处。
    out.append(f"{body} — [{source}]({url})" if url else body)
    return out


def _render_repo_item(item: Dict[str, Any]) -> List[str]:
    """一个开源项目：加粗标题 + 星标元信息 + 亮点 + 待办清单式部署指南。"""
    name = item.get("title", "")
    out = [f"**{name}**", ""]

    meta: List[str] = []
    if item.get("stars"):
        meta.append(f"★{_format_stars(item['stars'])}")
    if item.get("language"):
        meta.append(str(item["language"]))
    if item.get("is_new"):
        meta.append("近期趋势")
    if meta:
        out += [" · ".join(meta), ""]

    intro = repair_wikilinks(item.get("intro") or item.get("summary") or "")
    if intro:
        out += [intro, ""]

    features = [f for f in (item.get("features") or []) if f]
    if features:
        out += ["**核心亮点**", ""]
        out += [f"- {repair_wikilinks(f)}" for f in features]
        out.append("")

    guide = repair_wikilinks(item.get("guide") or "")
    steps = [s for s in (item.get("steps") or []) if s]
    install = item.get("install") or ""

    if guide or steps or install:
        out += ["**应用与部署指南**", ""]
        if guide:
            out += [guide, ""]

        # 固定用 "- [ ] " 前缀在这里拼出来，而不是让模型自己写：
        # 模型写清单语法时经常飘成 "* [ ]"、"1. [ ]" 甚至把它塞进正文里。
        for step in steps:
            out.append(f"- [ ] {repair_wikilinks(step)}")

        # 没有结构化步骤时，退化成把安装命令做成一条可勾选的任务，
        # 保证旧存档（没有 steps 字段的那几期）也有待办清单可用。
        if not steps and install:
            out.append(f"- [ ] {repair_wikilinks(install)}")

        out.append("")

    if install:
        # 命令已经在待办清单里出现过就不再单独贴代码块：模型经常把
        # install 原样也放进 steps，不去重的话同一行命令会在同一条目里出现两次。
        already_listed = any(
            step.strip().casefold() == install.strip().casefold() for step in steps
        )
        if not already_listed:
            out += ["```bash", install, "```", ""]

    if item.get("url"):
        out.append(f"[查看仓库]({item['url']})")

    return out


def render_markdown(report: Dict[str, Any], stats: Optional[Dict[str, Any]] = None) -> str:
    """
    把一份 report 渲染成完整的 Markdown 文本。

    纯函数：不读文件、不依赖时间，方便自检直接断言输出。
    """
    tags = normalize_tags(report.get("keywords"))
    lines = build_frontmatter(report, tags)
    lines.append("")

    digest = repair_wikilinks(report.get("digest", ""))
    if digest:
        lines += [f"> {digest}", ""]

    if report.get("degraded"):
        reason = report.get("degraded_reason") or "未知原因"
        # Obsidian 的 callout 语法，阅读视图里会渲染成一个醒目的提示框
        lines += [f"> [!warning] 本期为降级输出：{reason}", ""]

    for section in report.get("sections") or []:
        lines += [f"# {section.get('title', '未命名板块')}", ""]

        lead = repair_wikilinks(section.get("lead", ""))
        if lead:
            lines += [f"*{lead}*", ""]

        items = section.get("items") or []
        if not items:
            lines += ["（本时段无内容）", ""]
            continue

        is_github = section.get("key") == "github"
        for item in items:
            lines += _render_repo_item(item) if is_github else _render_news_item(item)
            lines.append("")

    # 收尾把多余空行压掉，并保证文件以单个换行结束（否则 Obsidian 会有一堆空段落）
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip("\n") + "\n"


def save_markdown(report: Dict[str, Any], stats: Optional[Dict[str, Any]] = None) -> Path:
    """把一份 report 落成 obsidian/YYYY-MM-DD.md。"""
    target = markdown_path(report.get("date", ""))
    config.OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(report, stats), encoding="utf-8")
    logger.info("已写入 Obsidian 笔记：%s", target)
    return target


def save_all(entries: Iterable[Dict[str, Any]]) -> int:
    """
    批量导出。入参是 renderer.load_archive() 的返回值（每项含 report / stats）。

    每期都重新生成一遍，不只生成当天：模板或双链规则升级后，往期笔记会跟着一起更新，
    不会留下一批旧格式的死文件。
    """
    written = 0
    for entry in entries:
        report = entry.get("report")
        if not isinstance(report, dict) or not report.get("date"):
            continue
        save_markdown(report, entry.get("stats") or {})
        written += 1
    return written


__all__ = [
    "strip_wikilinks",
    "repair_wikilinks",
    "extract_wikilinks",
    "strip_wikilinks_deep",
    "sanitize_tag",
    "coerce_keywords",
    "normalize_tags",
    "markdown_path",
    "build_frontmatter",
    "render_markdown",
    "save_markdown",
    "save_all",
]
