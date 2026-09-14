# -*- coding: utf-8 -*-
"""
DeepSeek 提炼层。

职责：把原始资讯交给 DeepSeek，产出结构化的五大板块内容。

工程要点：
- 强制 JSON 输出 + 宽松解析（模型偶尔会包 ```json 围栏）。
- URL 白名单校验：模型返回的链接必须能在原始素材里找到，防止幻觉链接。
- 失败降级：没有 API Key 或重试耗尽时，用原始素材直接生成兜底早报，保证网页正常产出。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import config
from fetcher import NewsItem, RepoItem, clean_text

logger = logging.getLogger(__name__)

try:  # openai 是可选依赖，缺失时走降级分支
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


# --------------------------------------------------------------------------
# 时间工具
# --------------------------------------------------------------------------
def local_now() -> datetime:
    """当前时间（Asia/Shanghai）。"""
    tz = ZoneInfo(config.TIMEZONE) if ZoneInfo else timezone(timedelta(hours=8))
    return datetime.now(tz)


_WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def today_local() -> date:
    return local_now().date()


def format_date_cn(d: date) -> str:
    return f"{d.strftime('%Y年%m月%d日')} {_WEEKDAY_CN[d.weekday()]}"


# --------------------------------------------------------------------------
# URL 归一化与白名单
# --------------------------------------------------------------------------
def normalize_url(url: str) -> str:
    """
    用于比对链接的归一化形式。

    规则：忽略协议、去掉 www.、去掉查询串与锚点、去掉末尾斜杠、域名转小写；
    GitHub 链接额外归一到仓库主页（去掉 /tree/main/... 之类的路径）。
    """
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower().rstrip("/")

    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    path = (parts.path or "").rstrip("/")

    if host == "github.com":
        segments = [s for s in path.split("/") if s]
        if len(segments) >= 2:
            path = f"/{segments[0]}/{segments[1]}"

    # 直接用字符串拼接而不是 urlunsplit：后者在没有 scheme 时会生成 "//host/path"
    return f"{host}{path}"


def _normalize_title(title: str) -> str:
    text = clean_text(title, limit=200).lower()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def is_foreign_text(text: str) -> bool:
    """判断一段文本是否以非中文为主（用于决定要不要显示"原文"一栏）。"""
    if not text:
        return False
    cjk = len(_CJK_RE.findall(text))
    # 中文字符占比低于 15% 就当作外文
    return cjk / max(len(text), 1) < 0.15


class UrlGuard:
    """
    模型输出的链接必须能对回原始素材，否则视为幻觉。

    顺便承担第二个职责：把原始素材里的**原文标题**和**原文摘要**带回来，
    供页面的中英对照使用。这些字段直接来自 RSS / GitHub，不经过模型，
    所以不存在被改写或编造的风险。
    """

    def __init__(self, news: List[NewsItem], repos: List[RepoItem]) -> None:
        self._by_url: Dict[str, Dict[str, str]] = {}
        self._by_title: Dict[str, Dict[str, str]] = {}

        for item in news:
            record = {
                "url": item.url,
                "source": item.source,
                "title_original": item.title,
                "excerpt": item.summary,
            }
            key = normalize_url(item.url)
            if key:
                self._by_url[key] = record
            title_key = _normalize_title(item.title)
            if title_key:
                self._by_title.setdefault(title_key, record)

        for repo in repos:
            record = {
                "url": repo.url,
                "source": "GitHub",
                "title_original": repo.full_name,
                "excerpt": repo.description,
            }
            key = normalize_url(repo.url)
            if key:
                self._by_url[key] = record
            # 仓库同样登记标题索引，这样模型把仓库写成 "owner/repo" 也能对回来
            repo_title_key = _normalize_title(repo.full_name)
            if repo_title_key:
                self._by_title.setdefault(repo_title_key, record)

    def resolve(self, url: str, title: str) -> Optional[Dict[str, str]]:
        """返回原始素材记录，或 None（表示无法确认真实性）。"""
        key = normalize_url(url) if url else ""
        if key and key in self._by_url:
            return self._by_url[key]

        title_key = _normalize_title(title) if title else ""
        if title_key:
            if title_key in self._by_title:
                return self._by_title[title_key]
            # 标题被模型改写过：尝试双向包含匹配
            for known_title, record in self._by_title.items():
                if len(title_key) >= 6 and (title_key in known_title or known_title in title_key):
                    return record

        return None


# --------------------------------------------------------------------------
# 候选素材挑选
# --------------------------------------------------------------------------
_BUCKETS = [
    ("cn", "tech"),
    ("intl", "tech"),
    ("cn", "consumer"),
    ("intl", "consumer"),
]


def select_candidates(news: List[NewsItem]) -> List[NewsItem]:
    """
    按 (region, category) 分桶后各取前 N 条，避免某个板块的原始素材被其他板块挤掉。
    """
    buckets: Dict[Tuple[str, str], List[NewsItem]] = {b: [] for b in _BUCKETS}
    others: List[NewsItem] = []

    for item in news:
        key = (item.region or "", item.category or "")
        if key in buckets:
            buckets[key].append(item)
        else:
            others.append(item)

    def sort_key(i: NewsItem):
        return i.published or datetime.min.replace(tzinfo=timezone.utc)

    selected: List[NewsItem] = []
    per_bucket = config.CANDIDATES_PER_SECTION

    for key in _BUCKETS:
        pool = sorted(buckets[key], key=sort_key, reverse=True)
        # 同源内容最多占一半，保证素材来源多样
        picked: List[NewsItem] = []
        source_counter: Dict[str, int] = {}
        cap = max(3, per_bucket // 2)
        for item in pool:
            if source_counter.get(item.source, 0) >= cap:
                continue
            source_counter[item.source] = source_counter.get(item.source, 0) + 1
            picked.append(item)
            if len(picked) >= per_bucket:
                break
        # 单源限制导致没取满时，放宽限制补齐
        if len(picked) < per_bucket:
            chosen_ids = {id(i) for i in picked}
            for item in pool:
                if id(item) in chosen_ids:
                    continue
                picked.append(item)
                if len(picked) >= per_bucket:
                    break
        selected.extend(picked)

    others.sort(key=sort_key, reverse=True)
    selected.extend(others[:per_bucket])

    return selected


def _fit_budget(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    控制送进模型的字符量，超预算时优先砍尾部条目。
    """
    budget = config.LLM_MAX_INPUT_CHARS

    def size() -> int:
        return len(json.dumps(payload, ensure_ascii=False))

    while size() > budget and payload["news"]:
        payload["news"].pop()
        if payload["news"] and size() > budget:
            payload["news"].pop()

    while size() > budget and payload["github"]:
        payload["github"].pop()

    if size() > budget:
        logger.warning("素材仍超出字符预算（%d > %d），继续截断摘要", size(), budget)
        for item in payload["news"]:
            item["raw_summary"] = item.get("raw_summary", "")[:120]

    return payload


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一名严谨的科技与消费行业资讯编辑，服务于一份每日早报。
你的唯一任务是把给定的原始资讯压缩、归类、重写成客观的中文简报。

你必须无条件遵守以下铁律：
1. 只能使用给定原始素材中的事实。禁止编造、脑补、外推任何事实、数字、公司名、人名或链接。
2. 客观陈述。禁止主观评论、价值判断、情绪化形容词、预测、投资建议和"值得关注"之类的话术。
3. 每条总结极度精简，控制在 60 个汉字以内，说清"谁 / 做了什么 / 关键结果或数据"即可。
4. 每条必须携带原始素材里真实存在的链接，原样复制，不得改写、拼接或臆造。
5. 只输出 JSON，不输出任何解释文字或 Markdown 代码块标记。"""


def _section_spec_text() -> str:
    lines = []
    for idx, sec in enumerate(config.SECTION_DEFS, start=1):
        lines.append(f"   {idx}) {sec['title']} —— JSON 键名 \"{sec['key']}\"")
    return "\n".join(lines)


def build_user_prompt(payload: Dict[str, Any], report_date: date) -> str:
    date_str = report_date.strftime("%Y-%m-%d")
    weekday = _WEEKDAY_CN[report_date.weekday()]
    per_section = config.ITEMS_PER_SECTION

    schema_example = {
        "date": date_str,
        "digest": "一句话导读，40 字以内，概括当日最重要的 2-3 件事",
        "sections": {
            "intl_tech": [
                {
                    "title": "中文标题，20 字以内",
                    "summary": "客观精简总结，60 字以内",
                    "source": "来源媒体名",
                    "url": "https://原始素材中的真实链接",
                }
            ],
            "cn_tech": [],
            "intl_consumer": [],
            "cn_consumer": [],
            "github": [
                {
                    "title": "owner/repo",
                    "summary": "项目做什么 + 为什么值得关注，60 字以内",
                    "source": "GitHub",
                    "url": "https://github.com/owner/repo",
                    "stars": "1234",
                    "language": "Python",
                }
            ],
        },
    }

    return f"""请把下面的原始素材整理成 {date_str}（{weekday}，Asia/Shanghai）的每日早报。

【板块要求】必须严格使用以下五个板块，不得增加、删除或改名：
{_section_spec_text()}

【分类口径】不设大公司专属板块。所有大厂动态——包括 AI、芯片、云服务、航天、自动驾驶、
具身智能、加密货币、算力基础设施等——一律归入对应的「国际科技新闻」或「国内科技新闻」，
判断依据是事件发生地与主体所在地。
「国际新消费新闻」「国内新消费新闻」聚焦零售、品牌、消费品、电商、冷链物流、餐饮、
服饰、美妆、出海消费、消费投融资等。

【数量】每个板块最多 {per_section} 条，按重要性从高到低排列。
原始素材不足以填满时，就给多少写多少，绝对禁止凑数或用无关内容填充。
素材明显不属于任何板块时直接丢弃。

【写作要求】
- 全部用中文输出。英文标题翻译成中文；GitHub 项目名保留英文原名 owner/repo。
- 每条 summary 控制在 60 个汉字以内，一句话，主语明确，包含关键数字。
- 同一个事件被多家媒体报道时，只保留一条，选信息量最大的那家作为来源。
- source 字段填原始素材里的媒体名 / 站点名；GitHub 条目固定填 "GitHub"。
- github 板块的 title 用 "owner/repo" 形式，summary 说明项目用途与亮点，stars 填原始星数。

【输出格式】只输出下面这个 JSON 对象，不要有任何前后缀文字：
{json.dumps(schema_example, ensure_ascii=False, indent=2)}

【原始素材】
{json.dumps(payload, ensure_ascii=False, indent=1)}
"""


# --------------------------------------------------------------------------
# 调用 DeepSeek
# --------------------------------------------------------------------------
def _parse_json_response(text: str) -> Dict[str, Any]:
    """宽松解析模型返回的 JSON。"""
    if not text or not text.strip():
        raise ValueError("模型返回空内容")

    cleaned = _FENCE_RE.sub("", text.strip()).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 兜底：截取第一个 { 到最后一个 } 之间的内容
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        snippet = cleaned[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败: {exc}") from exc

    raise ValueError("模型返回中找不到 JSON 对象")


def call_deepseek(payload: Dict[str, Any], report_date: date) -> Dict[str, Any]:
    """调用 DeepSeek，带指数退避重试。"""
    if OpenAI is None:
        raise RuntimeError("未安装 openai SDK，请执行 pip install openai")
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")

    client = OpenAI(
        api_key=config.DEEPSEEK_API_KEY,
        base_url=config.DEEPSEEK_BASE_URL,
        timeout=config.LLM_TIMEOUT_SECONDS,
        max_retries=0,  # 重试逻辑自己控制，便于记录日志
    )

    user_prompt = build_user_prompt(payload, report_date)
    logger.info("调用 DeepSeek，模型=%s，提示词长度=%d 字符", config.DEEPSEEK_MODEL, len(user_prompt))

    last_error: Optional[Exception] = None

    for attempt in range(1, config.LLM_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=config.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=config.LLM_TEMPERATURE,
                response_format={"type": "json_object"},
                stream=False,
            )

            content = response.choices[0].message.content or ""
            data = _parse_json_response(content)

            usage = getattr(response, "usage", None)
            if usage:
                logger.info(
                    "DeepSeek 返回成功（prompt=%s, completion=%s, total=%s）",
                    getattr(usage, "prompt_tokens", "?"),
                    getattr(usage, "completion_tokens", "?"),
                    getattr(usage, "total_tokens", "?"),
                )
            return data

        except Exception as exc:  # noqa: BLE001
            last_error = exc
            wait = min(2 ** attempt, 30)
            logger.warning("DeepSeek 第 %d/%d 次调用失败：%s", attempt, config.LLM_MAX_RETRIES, exc)
            if attempt < config.LLM_MAX_RETRIES:
                time.sleep(wait)

    raise RuntimeError(f"DeepSeek 调用在 {config.LLM_MAX_RETRIES} 次尝试后仍失败：{last_error}")


# --------------------------------------------------------------------------
# 结果归一化
# --------------------------------------------------------------------------
def _coerce_str(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    return clean_text(value, limit=limit)


def normalize_result(
    raw: Dict[str, Any],
    guard: UrlGuard,
    report_date: date,
) -> Dict[str, Any]:
    """
    把模型输出规整成页面需要的结构，并剔除无法验证链接的条目。
    """
    digest = _coerce_str(raw.get("digest"), limit=120)

    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, dict):
        # 模型有时会把板块平铺在顶层
        raw_sections = {k: v for k, v in raw.items() if k in config.SECTION_KEYS}

    sections: List[Dict[str, Any]] = []
    dropped = 0

    for sec_def in config.SECTION_DEFS:
        key = sec_def["key"]
        entries = raw_sections.get(key) or []
        if not isinstance(entries, list):
            entries = []

        items: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            title = _coerce_str(entry.get("title"), limit=120)
            summary = _coerce_str(entry.get("summary"), limit=260)
            if not title:
                continue

            resolved = guard.resolve(_coerce_str(entry.get("url"), limit=500), title)
            if resolved is None:
                dropped += 1
                logger.warning("丢弃链接无法验证的条目 [%s] %s", key, title)
                continue

            title_original = resolved.get("title_original", "")
            excerpt = resolved.get("excerpt", "")

            item = {
                "title": title,
                "summary": summary,
                "source": _coerce_str(entry.get("source"), limit=40) or resolved.get("source", ""),
                "url": resolved["url"],
                # 原文对照字段：直接来自 RSS / GitHub，未经模型改写
                "title_original": title_original,
                "excerpt": excerpt,
                "is_foreign": is_foreign_text(title_original) or is_foreign_text(excerpt),
            }

            if key == "github":
                item["stars"] = _coerce_str(entry.get("stars"), limit=20)
                item["language"] = _coerce_str(entry.get("language"), limit=30)
                # 仓库名本身就是原文标题，避免重复展示
                item["title_original"] = ""

            items.append(item)

        # 二次限流：防止模型超额输出
        items = items[: config.ITEMS_PER_SECTION]

        sections.append({**sec_def, "items": items})

    if dropped:
        logger.warning("共丢弃 %d 条链接无法验证的条目", dropped)

    return {"date": report_date.isoformat(), "digest": digest, "sections": sections}


# --------------------------------------------------------------------------
# 降级方案
# --------------------------------------------------------------------------
def build_fallback_result(
    news: List[NewsItem],
    repos: List[RepoItem],
    report_date: date,
    reason: str,
) -> Dict[str, Any]:
    """
    AI 不可用时，用原始素材拼一份朴素早报，保证页面与通知链路不被中断。
    """
    logger.warning("启用降级方案：%s", reason)

    buckets: Dict[Tuple[str, str], List[NewsItem]] = {b: [] for b in _BUCKETS}
    for item in news:
        key = (item.region or "", item.category or "")
        if key in buckets:
            buckets[key].append(item)

    section_items: Dict[str, List[Dict[str, Any]]] = {}
    for sec_key, bucket in (
        ("intl_tech", ("intl", "tech")),
        ("cn_tech", ("cn", "tech")),
        ("intl_consumer", ("intl", "consumer")),
        ("cn_consumer", ("cn", "consumer")),
    ):
        section_items[sec_key] = [
            {
                # 降级时没有译文，标题直接沿用原文
                "title": item.title,
                "summary": item.summary or "（AI 摘要不可用，请点击「阅读全文」查看原始报道）",
                "source": item.source,
                "url": item.url,
                "title_original": "",
                "excerpt": "",
                "is_foreign": is_foreign_text(item.title),
                "untranslated": True,
            }
            for item in buckets[bucket][: config.ITEMS_PER_SECTION]
        ]

    section_items["github"] = [
        {
            "title": repo.full_name,
            "summary": repo.description or "（AI 摘要不可用，仅展示项目描述）",
            "source": "GitHub",
            "url": repo.url,
            "stars": str(repo.stars),
            "language": repo.language,
            "title_original": "",
            "excerpt": "",
            "is_foreign": True,
            "untranslated": True,
        }
        for repo in repos[: config.ITEMS_PER_SECTION]
    ]

    sections = [
        {**sec_def, "items": section_items.get(sec_def["key"], [])}
        for sec_def in config.SECTION_DEFS
    ]

    return {
        "date": report_date.isoformat(),
        "digest": f"AI 摘要暂不可用（{reason}），以下为原始采集内容。",
        "sections": sections,
        "degraded": True,
        "degraded_reason": reason,
    }


# --------------------------------------------------------------------------
# 对外主入口
# --------------------------------------------------------------------------
def summarize(
    news: List[NewsItem],
    repos: List[RepoItem],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """完整提炼流程，任何异常都会退化为降级结果而不是抛出去。"""
    report_date = today_local()
    guard = UrlGuard(news, repos)

    if dry_run:
        return build_fallback_result(news, repos, report_date, "dry-run 模式")

    candidates = select_candidates(news)
    logger.info("送入模型的新闻候选 %d 条 / 仓库候选 %d 个", len(candidates), len(repos))

    payload = _fit_budget(
        {
            "report_date": report_date.isoformat(),
            "news": [item.to_prompt_dict() for item in candidates],
            "github": [repo.to_prompt_dict() for repo in repos],
        }
    )

    if not payload["news"] and not payload["github"]:
        return build_fallback_result(news, repos, report_date, "无可用原始素材")

    try:
        raw = call_deepseek(payload, report_date)
        result = normalize_result(raw, guard, report_date)

        total = sum(len(sec["items"]) for sec in result["sections"])
        if total == 0:
            raise ValueError("模型返回的所有条目都未通过链接校验")

        logger.info("AI 提炼完成，共 %d 条内容", total)
        return result

    except Exception as exc:  # noqa: BLE001
        logger.error("AI 提炼失败：%s", exc, exc_info=True)
        return build_fallback_result(news, repos, report_date, f"{type(exc).__name__}: {exc}")


__all__ = [
    "summarize",
    "today_local",
    "format_date_cn",
    "local_now",
    "normalize_url",
    "UrlGuard",
]
