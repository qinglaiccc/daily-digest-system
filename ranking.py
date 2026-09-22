# -*- coding: utf-8 -*-
"""
主编级前置筛选（纯本地，零网络调用）。

为什么单独一个模块：
    筛选规则是这套系统里最需要被反复调整、也最需要被断言覆盖的部分。把它做成
    不依赖网络、不依赖大模型的纯函数，就能在离线自检里把每条规则钉死，
    而不用每次都真的烧一次 DeepSeek 额度去验证"这条该不该被丢掉"。

本模块负责三道闸门：
    ① 硬否决    —— 与产业动态无关的类别（政治/军事/宏观/股市/司法/人事）直接丢弃
    ② 板块相关性 —— 条目必须命中所在板块的话题词，专治"宏观财经被塞进消费板块"
    ③ 本地加权   —— 正向词加分、数码评测类减分，产出排序用的分数

强触发白名单在第 ① ② ③ 之上：命中即无条件入选并豁免否决。
真正的语义判断交给 summarizer.rerank_candidates()（那一步才调 DeepSeek）。

依赖方向：只依赖 config 与 fetcher 的数据类，不 import summarizer / renderer，
所以可以被它们安全地反向 import。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import config
from fetcher import NewsItem

logger = logging.getLogger(__name__)

# 拉丁关键词的形态：整词/词组（可含空格、连字符、点、下划线、加号）
_LATIN_KW_RE = re.compile(r"^[a-z0-9][a-z0-9 +._-]*$")


@lru_cache(maxsize=None)
def _latin_pattern(keyword: str) -> Optional[re.Pattern]:
    """
    拉丁关键词编译成"词边界"正则；返回 None 表示这是中日韩关键词，走子串匹配。

    为什么必须区分：`ai` 这种两字母关键词如果按子串匹配，会命中 email、domain、
    available、captcha 里的一堆字符。实测这条规则直接影响候选池质量 ——
    按子串匹配时英文源几乎全都能过闸门，闸门形同虚设。
    中日韩文字没有词边界概念，只能用子串匹配。
    """
    kw = keyword.lower()
    if not _LATIN_KW_RE.match(kw):
        return None
    # (?<![a-z0-9]) / (?![a-z0-9]) 而不是 \b：\b 会把 "gpt-5" 这种带连字符的
    # 关键词切碎（- 与 5 之间也算词边界），导致匹配范围比预期宽。
    return re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")


def matches(text: str, keyword: str) -> bool:
    """统一的命中判定：拉丁词按词边界，中文词按子串。"""
    pattern = _latin_pattern(keyword)
    if pattern is not None:
        return pattern.search(text) is not None
    return keyword.lower() in text


def any_match(text: str, keywords: Iterable[str]) -> List[str]:
    """返回所有命中的关键词（用于强触发词，需要知道具体命中了什么）。"""
    return [kw for kw in keywords if matches(text, kw)]

# (region, category) -> 板块 key
_SECTION_MAP: Dict[Tuple[str, str], str] = {
    ("intl", "tech"): "intl_tech",
    ("cn", "tech"): "cn_tech",
    ("intl", "consumer"): "intl_consumer",
    ("cn", "consumer"): "cn_consumer",
}

# 新闻板块的顺序（与 config.SECTION_DEFS 里的新闻板块一致）
NEWS_SECTIONS: List[str] = list(_SECTION_MAP.values())


@dataclass
class ScoredItem:
    """一条带评分与判定依据的候选。notes 用于日志排查与自检断言。"""

    item: NewsItem
    section: str
    score: float = 0.0
    forced: bool = False
    force_terms: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 基础判定
# --------------------------------------------------------------------------
def blob(item: NewsItem) -> str:
    """用于匹配的统一文本：标题 + 摘要，统一小写。"""
    return f"{item.title or ''} {item.summary or ''}".lower()


def force_hits(item: NewsItem) -> List[str]:
    """返回命中的强触发词。非空即代表"无条件入选"。"""
    return any_match(blob(item), config.GEEK_FORCE_KEYWORDS)


def veto_hit(item: NewsItem) -> str:
    """
    返回命中的硬否决词；空串代表不否决。

    命中强触发词的条目默认豁免否决（可配置），理由是：漏掉一条硬核新闻的代价
    远高于多收一条带点噪音的新闻。用户抱怨的正是"漏"，所以这里宁可放宽。
    """
    if config.GEEK_FORCE_BYPASSES_VETO and force_hits(item):
        return ""

    hits = any_match(blob(item), config.CONTENT_VETO_KEYWORDS)
    return hits[0] if hits else ""


def section_hint_hit(item: NewsItem, section: str) -> List[str]:
    """返回命中的板块话题词。非空代表这条内容确实属于该板块。"""
    hints = config.SECTION_HINTS.get(section) or []
    return any_match(blob(item), hints)


def section_of(item: NewsItem) -> str:
    """按源的 region/category 归属板块；缺失时退回到 intl_tech。"""
    return _SECTION_MAP.get((item.region or "", item.category or ""), "intl_tech")


def _freshness_bonus(item: NewsItem) -> float:
    """越新越靠前。6 小时内 +4，12 小时内 +2，24 小时内 +1，更早 0。"""
    if not item.published:
        return 0.0
    age = datetime.now(timezone.utc) - item.published
    hours = age.total_seconds() / 3600
    if hours <= 6:
        return 4.0
    if hours <= 12:
        return 2.0
    if hours <= 24:
        return 1.0
    return 0.0


def local_score(item: NewsItem) -> Tuple[float, List[str]]:
    """
    本地加权评分。返回 (分数, 命中说明)。

    刻意做得简单可解释：正向词加权重、负向词减权重、新鲜度加权。
    复杂的打分模型在这个场景不划算 —— 真正需要"懂内容"的判断交给 LLM 重排，
    本地这层的职责只是把明显该靠前的靠前、明显该靠后的靠后。
    """
    text = blob(item)
    score = 0.0
    notes: List[str] = []

    for kw, weight in config.POSITIVE_KEYWORDS.items():
        if matches(text, kw):
            score += weight
            notes.append(f"+{weight} {kw}")

    for kw, weight in config.NEGATIVE_KEYWORDS.items():
        if matches(text, kw):
            score -= weight
            notes.append(f"-{weight} {kw}")

    bonus = _freshness_bonus(item)
    if bonus:
        score += bonus
        notes.append(f"+{bonus:g} 新鲜")

    return score, notes


# --------------------------------------------------------------------------
# 池子构建
# --------------------------------------------------------------------------
def build_pool(
    news: Sequence[NewsItem],
    *,
    pool_per_section: Optional[int] = None,
) -> Dict[str, List[ScoredItem]]:
    """
    把原始新闻筛成"按板块分组的候选池"。

    逐个条目：定板块 → 硬否决 → 板块相关性 → 评分 → 入池。
    最后每个板块按 (是否强触发, 分数) 倒序排列，截到 pool_per_section ——
    但强触发条目**不受截断影响**，一定会留在池子里（这正是"防漏网之鱼"的落点：
    保证写在代码里，而不是指望提示词里的一句"请不要漏掉"）。
    """
    cap = pool_per_section if pool_per_section is not None else config.RERANK_POOL_PER_SECTION

    pools: Dict[str, List[ScoredItem]] = {key: [] for key in NEWS_SECTIONS}
    stats = {"vetoed": 0, "irrelevant": 0, "forced": 0}

    for item in news:
        section = section_of(item)
        hits = force_hits(item)
        forced = bool(hits)

        reason = veto_hit(item)
        if reason:
            stats["vetoed"] += 1
            logger.debug("硬否决（%s）：%s", reason, item.title[:40])
            continue

        if not forced and not section_hint_hit(item, section):
            # 相关性闸门：这条内容和它被分到的板块对不上（典型症状是宏观财经
            # 被源的 category 带进了消费板块）。强触发条目豁免。
            stats["irrelevant"] += 1
            logger.debug("板块不匹配（%s）：%s", section, item.title[:40])
            continue

        score, notes = local_score(item)
        if forced:
            stats["forced"] += 1
            notes.insert(0, "强制入选：" + "、".join(hits))
            score += 100.0  # 排序时压过一切

        pools[section].append(
            ScoredItem(
                item=item,
                section=section,
                score=score,
                forced=forced,
                force_terms=hits,
                notes=notes,
            )
        )

    for key, entries in pools.items():
        entries.sort(key=lambda e: (e.forced, e.score), reverse=True)
        if len(entries) > cap:
            kept_forced = [e for e in entries if e.forced]
            kept_rest = [e for e in entries if not e.forced][: max(0, cap - len(kept_forced))]
            pools[key] = sorted(kept_forced + kept_rest, key=lambda e: (e.forced, e.score), reverse=True)

    logger.info(
        "本地筛选：硬否决 %d 条，板块不匹配 %d 条，强触发 %d 条；入池 %s",
        stats["vetoed"],
        stats["irrelevant"],
        stats["forced"],
        {k: len(v) for k, v in pools.items()},
    )

    # 空池必须显式报警。相关性闸门配得太严时，某个板块会被静默清空 ——
    # 页面上只会显示"本时段无内容"，没有任何迹象指向筛选规则，极难排查。
    for key, entries in pools.items():
        if not entries:
            logger.warning(
                "板块 [%s] 候选池为空：可能是该板块的源全部失效，或 SECTION_HINTS 缺少对应话题词",
                key,
            )

    return pools


# --------------------------------------------------------------------------
# 最终选择
# --------------------------------------------------------------------------
def fallback_selection(
    pool: Dict[str, List[ScoredItem]],
    keep: Optional[int] = None,
) -> Dict[str, List[ScoredItem]]:
    """
    纯本地选择：每个板块取分数最高的前 keep 条（强触发条目优先占位）。

    LLM 重排不可用时走这条路径。因为没有语义判断，只能靠本地分数，
    所以**强触发条目的保底名额在这条路径上尤其重要** —— 它是"硬核新闻不被漏掉"
    的最后一道保障。
    """
    limit = keep if keep is not None else config.RERANK_KEEP_PER_SECTION
    return {key: entries[:limit] for key, entries in pool.items()}


def merge_llm_selection(
    pool: Dict[str, List[ScoredItem]],
    picks: Dict[str, Iterable[int]],
    keep: Optional[int] = None,
) -> Dict[str, List[ScoredItem]]:
    """
    合并 LLM 的重排结果，并强制把强触发条目塞回去。

    picks 是 {板块: [池内下标, ...]}，由 summarizer 从模型返回里解析。
    下标的容错（越界、非整数、重复）都在这里兜掉，不信任模型给的任何数字。

    关键点：**模型没有最终否决权。** 即使它把某条强触发条目排除了，
    这里也会按保底名额把它补回来 —— 用户要求"无视任何评分逻辑强制入选"，
    这个保证必须落在代码里。
    """
    limit = keep if keep is not None else config.RERANK_KEEP_PER_SECTION
    result: Dict[str, List[ScoredItem]] = {}

    for key, entries in pool.items():
        chosen: List[ScoredItem] = []
        seen: set = set()

        # 1) 强触发条目按保底名额先行占位
        forced_entries = [e for e in entries if e.forced]
        for entry in forced_entries[: config.FORCE_RESERVED_PER_SECTION]:
            chosen.append(entry)
            seen.add(id(entry))

        # 2) 采纳模型的选择（下标做边界检查）
        raw_picks = list(picks.get(key) or [])
        for index in raw_picks:
            if not isinstance(index, int) or isinstance(index, bool):
                continue
            if index < 0 or index >= len(entries):
                continue
            entry = entries[index]
            if id(entry) in seen:
                continue
            chosen.append(entry)
            seen.add(id(entry))
            if len(chosen) >= limit:
                break

        # 3) 模型给的不够（或全给了非法下标）时，用本地分数补齐
        if len(chosen) < limit:
            for entry in entries:
                if id(entry) in seen:
                    continue
                chosen.append(entry)
                seen.add(id(entry))
                if len(chosen) >= limit:
                    break

        result[key] = chosen[:limit]

    return result


def selected_sections(
    selection: Dict[str, List[ScoredItem]],
) -> Dict[str, List[Dict[str, Any]]]:
    """把选择结果转成 prompt 用的字典（送进模型时只带必要字段）。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, entries in selection.items():
        out[key] = [
            {
                "index": idx,
                "title": e.item.title,
                "source": e.item.source,
                "url": e.item.url,
                "raw_summary": e.item.summary,
            }
            for idx, e in enumerate(entries)
        ]
    return out


def summarize_selection(selection: Dict[str, List[ScoredItem]]) -> str:
    """给日志用的一句话摘要。"""
    parts = []
    for key, entries in selection.items():
        forced = sum(1 for e in entries if e.forced)
        parts.append(f"{key} {len(entries)} 条" + (f"（含强触发 {forced}）" if forced else ""))
    return "；".join(parts)


__all__ = [
    "ScoredItem",
    "NEWS_SECTIONS",
    "blob",
    "force_hits",
    "veto_hit",
    "section_hint_hit",
    "section_of",
    "local_score",
    "build_pool",
    "fallback_selection",
    "merge_llm_selection",
    "selected_sections",
    "summarize_selection",
]
