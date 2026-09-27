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

import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import config
from fetcher import NewsItem, dedup_url_key, normalize_title_for_dedup

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
# 跨天去重
# --------------------------------------------------------------------------
def _seen_index(
    records: Sequence[Dict[str, Any]],
    today: str,
) -> Tuple[Set[str], List[Tuple[str, str]]]:
    """
    把 seen 记录整理成两个查询结构：(URL 哈希集合, [(归一化标题, 日期)])。

    当天自己的记录会被剔除：force 重跑时当天内容已在 seen 里，
    不剔除的话整期早报会被自己拦成 0 条。
    """
    hashes: Set[str] = set()
    titles: List[Tuple[str, str]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("d") == today:
            continue
        url_hash = record.get("u")
        if isinstance(url_hash, str) and url_hash:
            hashes.add(url_hash)
        title = record.get("t")
        if isinstance(title, str) and title:
            titles.append((title, str(record.get("d") or "")))
    return hashes, titles


def _title_seen_before(
    norm_title: str,
    seen_titles: Sequence[Tuple[str, str]],
    threshold: float,
) -> Optional[str]:
    """
    判断标题是否与已发内容重复。返回命中的已发标题；None 表示不重复。

    两级匹配：归一化后精确相等 → 直接判重；否则模糊相似度（带长度预过滤，
    避免 300 条候选 × 200 条历史的全量 SequenceMatcher）。
    """
    if not norm_title:
        return None
    for seen_title, _day in seen_titles:
        if norm_title == seen_title:
            return seen_title
        # 预过滤：长度差太大不可能是同一条新闻的改写
        if abs(len(norm_title) - len(seen_title)) > 20:
            continue
        if difflib.SequenceMatcher(None, norm_title, seen_title).ratio() >= threshold:
            return seen_title
    return None


def is_seen_before(
    item: NewsItem,
    seen_hashes: Set[str],
    seen_titles: Sequence[Tuple[str, str]],
    today: str,
) -> Optional[str]:
    """返回重复原因描述（用于日志）；None 表示不是重复内容。"""
    url_hash = dedup_url_key(item.url)
    if url_hash and url_hash in seen_hashes:
        return "URL 已推送过"
    norm_title = normalize_title_for_dedup(item.title)
    hit = _title_seen_before(
        norm_title, seen_titles, config.SEEN_TITLE_SIMILARITY
    )
    if hit:
        return f"标题与已发内容相似（历史：{hit[:24]}…）"
    return None


# --------------------------------------------------------------------------
# 源族与配额
# --------------------------------------------------------------------------
def source_family(source: str) -> str:
    """源的所属族。同一媒体的多条 feed 归为一族，共享板块配额。"""
    key = (source or "").strip().lower()
    return config.SOURCE_FAMILIES.get(key, key or "未知来源")


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
    seen: Optional[Sequence[Dict[str, Any]]] = None,
    today: Optional[str] = None,
) -> Dict[str, List[ScoredItem]]:
    """
    把原始新闻筛成"按板块分组的候选池"。

    逐个条目：定板块 → 跨天去重 → 硬否决 → 板块相关性 → 评分 → 入池。
    最后每个板块按 (是否强触发, 分数) 倒序排列，截到 pool_per_section ——
    但强触发条目**不受截断影响**，一定会留在池子里（这正是"防漏网之鱼"的落点：
    保证写在代码里，而不是指望提示词里的一句"请不要漏掉"）。

    seen 是跨天去重历史（fetcher.load_seen_articles 的 records）。去重闸门
    排在最前面，并且**强触发词不豁免它**：强触发豁免的是"评分逻辑"（别漏），
    而"已经推送过的内容"不是评分问题 —— 重复推送本身就是用户明确要消灭的问题。
    """
    cap = pool_per_section if pool_per_section is not None else config.RERANK_POOL_PER_SECTION

    today = today or ""
    seen_hashes, seen_titles = _seen_index(seen or [], today)

    pools: Dict[str, List[ScoredItem]] = {key: [] for key in NEWS_SECTIONS}
    stats = {"seen": 0, "vetoed": 0, "irrelevant": 0, "forced": 0}

    for item in news:
        section = section_of(item)

        # 闸门⓪：跨天去重。已经推送过的内容，无论多重磅都不再进第二天的版面。
        seen_reason = is_seen_before(item, seen_hashes, seen_titles, today)
        if seen_reason:
            stats["seen"] += 1
            logger.debug("跨天去重（%s）：%s", seen_reason, item.title[:40])
            continue

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
        "本地筛选：跨天去重 %d 条，硬否决 %d 条，板块不匹配 %d 条，强触发 %d 条；入池 %s",
        stats["seen"],
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
def finalize_selection(
    pool: Dict[str, List[ScoredItem]],
    picks: Optional[Dict[str, Iterable[int]]] = None,
    keep: Optional[int] = None,
) -> Dict[str, List[ScoredItem]]:
    """
    最终选定：强触发保底 → 主编顺序 → 源族配额 → 弹性截断。

    四条规则，优先级从高到低：
    1. 强触发条目按保底名额（FORCE_RESERVED_PER_SECTION）先行占位，豁免源族配额；
       超出保底名额的强触发条目与其他条目一样受配额约束。
    2. picks 给出主编的优先顺序（池内下标）。下标容错（越界/非整数/重复）在这里兜掉。
    3. 源族配额：同一源族在同一板块最多 SOURCE_CAP_PER_SECTION 条 —— 被 config 注释里
       那个"HF 榜单霸屏"问题就是这条治的。配额拦下主编的选择时，从池中其余条目
       （按本地排序）顺延补位，把名额让给别的源。
    4. 弹性（宁缺毋滥）：主编没选够时**不回填** —— 它只挑了 2 条就出 2 条，
       版面空着也不注水。唯一的例外是第 3 条的补位（那是"换来源"而不是"凑数量"）。

    picks=None 表示 LLM 重排不可用：此时主编顺序退化为池子的本地排序
    （下标 0..n-1 全部有效），行为等同于旧的纯本地选择，但同样受配额与弹性约束。
    """
    limit = keep if keep is not None else config.RERANK_KEEP_PER_SECTION
    result: Dict[str, List[ScoredItem]] = {}

    for key, entries in pool.items():
        if not entries:
            result[key] = []
            continue

        raw_picks = list(picks.get(key) or []) if picks else []
        if picks is None:
            # 重排不可用：本地排序就是主编顺序
            raw_picks = list(range(len(entries)))

        # 主编顺序（去重、保序、边界检查）
        ordered: List[ScoredItem] = []
        seen_ids: Set[int] = set()
        for index in raw_picks:
            if not isinstance(index, int) or isinstance(index, bool):
                continue
            if index < 0 or index >= len(entries):
                continue
            entry = entries[index]
            if id(entry) in seen_ids:
                continue
            seen_ids.add(id(entry))
            ordered.append(entry)

        # 模型给了下标但全是非法值（越界/脏类型）——这是输出损坏的信号，
        # 不是"主编明确不选"。退回本地序。区别于 picks 为空数组：那是主编
        # 明确表达"这个板块今天没有值得报的"，弹性生效，尊重它。
        if raw_picks and not ordered:
            logger.warning("板块 [%s] 的重排下标全部非法（%s），退回本地排序",
                           key, raw_picks[:10])
            ordered = list(entries)

        chosen: List[ScoredItem] = []
        chosen_ids: Set[int] = set()
        family_count: Dict[str, int] = {}
        forced_reserved = 0
        blocked_by_quota = 0

        def _admit(entry: ScoredItem) -> bool:
            """尝试收录一条；返回是否收录。配额拦截时返回 False。"""
            nonlocal forced_reserved, blocked_by_quota
            family = source_family(entry.item.source)
            # 强触发条目在保底名额内豁免配额（它们是罕见事件，优先级最高）
            if entry.forced and forced_reserved < config.FORCE_RESERVED_PER_SECTION:
                forced_reserved += 1
            elif family_count.get(family, 0) >= config.SOURCE_CAP_PER_SECTION:
                blocked_by_quota += 1
                return False
            family_count[family] = family_count.get(family, 0) + 1
            chosen.append(entry)
            chosen_ids.add(id(entry))
            return True

        # 1) 强触发条目按保底名额先行占位（在主编顺序之前，保证它们一定进得来）
        for entry in entries:
            if len(chosen) >= limit:
                break
            if entry.forced and id(entry) not in chosen_ids:
                _admit(entry)

        # 2) 走主编顺序
        for entry in ordered:
            if len(chosen) >= limit:
                break
            if id(entry) in chosen_ids:
                continue
            _admit(entry)

        # 3) 配额拦下过主编的选择时，从池中其余条目按本地排序顺延补位。
        #    注意这只发生在"主编的选择被配额挤掉"的场合 —— 主编本来就选得少
        #    （弹性）时 blocked_by_quota == 0，不会触发回填。
        if picks is not None and blocked_by_quota and len(chosen) < limit:
            for entry in entries:
                if len(chosen) >= limit:
                    break
                if id(entry) in chosen_ids:
                    continue
                _admit(entry)

        result[key] = chosen

    return result


def fallback_selection(
    pool: Dict[str, List[ScoredItem]],
    keep: Optional[int] = None,
) -> Dict[str, List[ScoredItem]]:
    """纯本地选择（LLM 重排不可用时的降级路径）。带源族配额，无弹性截断。"""
    return finalize_selection(pool, picks=None, keep=keep)


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
    "is_seen_before",
    "source_family",
    "build_pool",
    "finalize_selection",
    "fallback_selection",
    "selected_sections",
    "summarize_selection",
]
