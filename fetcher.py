# -*- coding: utf-8 -*-
"""
数据采集层。

职责：
1. fetch_all_news()      —— 并发抓取配置里的全部 RSS 源，按时间窗过滤。
2. fetch_github_trending() —— 调用 GitHub Search API 抓取近 24 小时活跃的高星项目。

设计要点：
- 单个源失败绝不影响整体流程，失败信息收集到 FetchReport.errors 里。
- 所有网络请求共用带重试的 Session；RSS 用线程池并发，降低整体耗时。
"""

from __future__ import annotations

import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import feedparser
import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 v1 / v2 兼容
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    Retry = None  # type: ignore

import config

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class NewsItem:
    """一条已归一化的新闻。"""

    title: str
    url: str
    source: str
    summary: str = ""
    published: Optional[datetime] = None
    region: str = ""        # intl / cn
    category: str = ""      # tech / consumer

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published": (
                self.published.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                if self.published
                else "未知"
            ),
            "region_hint": self.region or "未知",
            "category_hint": self.category or "未知",
            "raw_summary": self.summary,
        }


@dataclass
class RepoItem:
    """一个 GitHub 仓库。"""

    full_name: str
    url: str
    description: str = ""
    stars: int = 0
    language: str = ""
    topics: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    pushed_at: Optional[datetime] = None
    is_new: bool = False

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "repo": self.full_name,
            "url": self.url,
            "description": self.description,
            "stars": self.stars,
            "language": self.language or "未标注",
            "topics": self.topics[:8],
            "created": self.created_at.strftime("%Y-%m-%d") if self.created_at else "未知",
            "last_push": self.pushed_at.strftime("%Y-%m-%d %H:%M UTC") if self.pushed_at else "未知",
            "is_new_in_24h": self.is_new,
        }


@dataclass
class FetchReport:
    """采集过程的诊断信息，用于日志和页面底部统计。"""

    ok: int = 0                       # 成功的源 / 检索次数
    failed: int = 0                   # 失败的源 / 检索次数
    items: int = 0                    # 命中的条目数
    label: str = "源"
    errors: List[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        if len(self.errors) < 40:
            self.errors.append(message)

    @property
    def summary(self) -> str:
        return (
            f"{self.label} 成功 {self.ok} / 失败 {self.failed}，"
            f"命中 {self.items} 条，异常 {len(self.errors)} 条"
        )


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def build_session() -> requests.Session:
    """构造带有重试与默认请求头的 Session。"""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )

    if Retry is not None:
        retry = Retry(
            total=config.HTTP_RETRIES,
            backoff_factor=1.2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    else:  # pragma: no cover
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)

    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def clean_text(raw: Any, limit: int = 400) -> str:
    """去掉 HTML 标签、解码实体、压缩空白，并截断到 limit 字符。"""
    if not raw:
        return ""
    text = str(raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    text = text.strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _struct_to_dt(struct_time: Any) -> Optional[datetime]:
    """把 feedparser 的 struct_time 转成带 UTC 时区的 datetime。"""
    if not struct_time:
        return None
    try:
        return datetime(*struct_time[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """解析 GitHub 返回的 ISO8601 时间（形如 2026-09-13T00:00:00Z）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_entry_link(entry: Any) -> str:
    """尽力从 entry 里取出一个可点击的原文链接。"""
    link = entry.get("link")
    if link:
        return str(link).strip()

    links = entry.get("links") or []
    for candidate in links:
        href = candidate.get("href")
        if href and candidate.get("rel") in (None, "alternate"):
            return str(href).strip()

    return str(entry.get("id") or "").strip()


# --------------------------------------------------------------------------
# RSS 抓取
# --------------------------------------------------------------------------
def fetch_rss_source(
    source: Dict[str, Any],
    session: requests.Session,
    since: datetime,
) -> List[NewsItem]:
    """抓取单个 RSS 源，返回时间窗内的条目。"""
    name = source["name"]
    url = source["url"]

    response = session.get(url, timeout=config.HTTP_TIMEOUT)
    response.raise_for_status()

    # 让 feedparser 自己猜编码，避免中文源乱码
    parsed = feedparser.parse(response.content)

    if not parsed.version and not parsed.entries:
        # 典型情况：站点已下线 RSS，返回的是 HTML 页面或 WAF 拦截页
        content_type = response.headers.get("content-type", "unknown").split(";")[0]
        raise ValueError(
            f"响应不是有效的 RSS/Atom（content-type={content_type}），该源可能已下线或改版"
        )

    if parsed.bozo and not parsed.entries:
        raise ValueError(f"feed 解析失败: {getattr(parsed, 'bozo_exception', 'unknown')}")

    entries = parsed.entries or []
    if not entries:
        raise ValueError("feed 中没有条目（可能已停止更新或需要更换源）")

    dated: List[NewsItem] = []
    undated: List[NewsItem] = []

    for entry in entries[:80]:  # 单源最多看 80 条，避免个别大源拖慢速度
        title = clean_text(entry.get("title"), limit=200)
        link = _extract_entry_link(entry)
        if not title or not link:
            continue

        published = _struct_to_dt(entry.get("published_parsed")) or _struct_to_dt(
            entry.get("updated_parsed")
        )

        body = entry.get("summary") or entry.get("description") or ""
        if not body:
            content = entry.get("content") or []
            if content and isinstance(content, list):
                body = content[0].get("value", "")

        item = NewsItem(
            title=title,
            url=link,
            source=name,
            summary=clean_text(body, limit=320),
            published=published,
            region=source.get("region", ""),
            category=source.get("category", ""),
        )

        if published is None:
            undated.append(item)
        elif published >= since:
            dated.append(item)

    # 有些源天生不带时间戳。仅当该源"几乎没有可用时间"时，才降级采用其无时间条目，
    # 否则一律按"无法确认在 24 小时内"丢弃。
    if not dated and undated:
        logger.warning("[%s] 全部条目缺少时间戳，降级采用前 %d 条", name, 5)
        dated = undated[:5]

    dated.sort(key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return dated


def fetch_all_news(session: Optional[requests.Session] = None) -> tuple[List[NewsItem], FetchReport]:
    """并发抓取全部启用的 RSS 源。"""
    session = session or build_session()
    report = FetchReport(label="RSS 源")

    since = datetime.now(timezone.utc) - timedelta(hours=config.LOOKBACK_HOURS)
    sources = [s for s in config.RSS_SOURCES if s.get("enabled")]

    logger.info("开始抓取 %d 个 RSS 源（时间窗：%s 之后）", len(sources), since.isoformat())

    collected: List[NewsItem] = []
    seen_urls: set[str] = set()

    with ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {
            executor.submit(fetch_rss_source, src, session, since): src for src in sources
        }
        for future in as_completed(future_map):
            src = future_map[future]
            try:
                items = future.result()
            except Exception as exc:  # noqa: BLE001 —— 单源失败必须被吞掉
                report.failed += 1
                report.add_error(f"{src['name']}: {type(exc).__name__}: {exc}")
                logger.warning("RSS 源失败 [%s] %s", src["name"], exc)
                continue

            report.ok += 1
            added = 0
            for item in items:
                key = item.url.split("?")[0].rstrip("/")
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                collected.append(item)
                added += 1
            report.items += added
            logger.info("RSS 源成功 [%s] 新增 %d 条", src["name"], added)

    logger.info("RSS 采集完成：%s", report.summary)
    return collected, report


# --------------------------------------------------------------------------
# GitHub 采集
# --------------------------------------------------------------------------
class GitHubAuthError(RuntimeError):
    """GITHUB_TOKEN 缺失或无效 —— 这类问题重试没有意义，直接给出可操作的提示。"""


class GitHubRateLimitError(RuntimeError):
    """触发 GitHub 限流 —— 可等待配额重置后重试。"""

    def __init__(self, message: str, reset_at: Optional[datetime] = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


def _github_headers() -> Dict[str, str]:
    """
    构造 GitHub API 请求头。

    强制要求携带 GITHUB_TOKEN：匿名调用 Search API 的配额只有 10 次/分钟，
    本项目的 8 次检索稍有并发就会被打满，导致整个「GitHub 热门项目」板块空掉。
    带 token 后配额提升到 30 次/分钟，这才是可靠运行的前提。
    """
    if not config.GITHUB_TOKEN:
        raise GitHubAuthError(
            "缺少 GITHUB_TOKEN。请在 GitHub 仓库的 Settings → Secrets and variables → Actions "
            "中添加名为 GITHUB_TOKEN 的 Secret（或使用工作流自动注入的同名变量）。"
            "匿名调用 GitHub Search API 只有 10 次/分钟配额，无法稳定支撑 8 次检索。"
        )

    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
    }


def _rate_limit_reset_at(response: "requests.Response") -> Optional[datetime]:
    raw = response.headers.get("X-RateLimit-Reset")
    if raw and raw.isdigit():
        return datetime.fromtimestamp(int(raw), timezone.utc)
    return None


def _search_repositories(
    session: requests.Session,
    query: str,
    sort: str = "stars",
    max_attempts: int = 3,
) -> List[Dict[str, Any]]:
    """
    调用一次 GitHub Search API。

    异常分级处理：
      - 401/403 且提示 token 无效 → GitHubAuthError，立即失败（重试无意义）
      - 403/429 限流               → 短退避后重试，最终抛出 GitHubRateLimitError
      - 5xx / 网络抖动             → 退避重试
      - 422 查询语法问题           → 直接抛错，不重试
    """
    headers = _github_headers()
    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        response = None
        try:
            response = session.get(
                "https://api.github.com/search/repositories",
                params={
                    "q": query,
                    "sort": sort,
                    "order": "desc",
                    "per_page": min(config.GITHUB_PER_TOPIC_LIMIT, 50),
                },
                headers=headers,
                timeout=config.HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = exc
            logger.warning("GitHub 请求异常（第 %d/%d 次）：%s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(min(2 ** attempt, 10))
            continue

        # ---- 成功 ----
        if response.status_code == 200:
            try:
                return response.json().get("items", []) or []
            except ValueError as exc:
                last_error = exc
                logger.warning("GitHub 返回体不是合法 JSON（第 %d 次）：%s", attempt, exc)
                continue

        # ---- 认证问题：重试没有意义 ----
        if response.status_code == 401:
            raise GitHubAuthError(
                "GITHUB_TOKEN 无效或已过期（HTTP 401）。请重新生成 Token 并更新 Secret。"
            )

        if response.status_code == 403:
            body = response.text[:200]
            remaining = response.headers.get("X-RateLimit-Remaining", "?")

            # token 权限不足也会返回 403，通过响应体区分
            if "rate limit" not in body.lower() and remaining not in ("0", "?"):
                raise GitHubAuthError(
                    f"GITHUB_TOKEN 权限不足或被拒绝（HTTP 403）：{body}"
                )

            reset_at = _rate_limit_reset_at(response)
            hint = f"，配额将于 {reset_at:%H:%M UTC} 重置" if reset_at else ""
            last_error = GitHubRateLimitError(
                f"GitHub Search API 触发限流（remaining={remaining}{hint}）", reset_at
            )
            logger.warning("GitHub 限流（第 %d/%d 次）%s", attempt, max_attempts, hint)

            # 限流要等得久一点，但如果配额重置时间还很远就没必要干等
            if attempt < max_attempts:
                wait = min(2 ** attempt * 3, 30)
                if reset_at:
                    seconds_to_reset = (reset_at - datetime.now(timezone.utc)).total_seconds()
                    if seconds_to_reset > 60:
                        logger.warning("配额重置还需 %.0f 秒，放弃本次等待", seconds_to_reset)
                        break
                time.sleep(wait)
            continue

        if response.status_code == 422:
            raise RuntimeError(f"GitHub 查询语法被拒绝（HTTP 422）：{query}")

        if response.status_code >= 500:
            last_error = RuntimeError(f"GitHub 服务端错误 HTTP {response.status_code}")
            logger.warning(
                "GitHub 服务端错误 %d（第 %d/%d 次）", response.status_code, attempt, max_attempts
            )
            if attempt < max_attempts:
                time.sleep(min(2 ** attempt, 10))
            continue

        # 其它未预期状态码
        last_error = RuntimeError(f"GitHub 返回未预期的 HTTP {response.status_code}")
        logger.warning("GitHub 未预期状态码 %d（第 %d 次）", response.status_code, attempt)
        if attempt < max_attempts:
            time.sleep(min(2 ** attempt, 8))

    raise last_error or RuntimeError("GitHub 检索失败：未知原因")


def _to_repo_item(raw: Dict[str, Any], is_new: bool) -> RepoItem:
    return RepoItem(
        full_name=raw.get("full_name", ""),
        url=raw.get("html_url", ""),
        description=clean_text(raw.get("description"), limit=220),
        stars=int(raw.get("stargazers_count") or 0),
        language=raw.get("language") or "",
        topics=list(raw.get("topics") or []),
        created_at=_parse_iso(raw.get("created_at")),
        pushed_at=_parse_iso(raw.get("pushed_at")),
        is_new=is_new,
    )


def _is_relevant_repo(repo: RepoItem) -> bool:
    """过滤掉明显与"效率 / AI 工具"主题无关的仓库。"""
    if not repo.full_name or not repo.url:
        return False
    if repo.stars < config.GITHUB_MIN_STARS:
        return False

    blob = f"{repo.full_name} {repo.description}".lower()
    for bad in config.GITHUB_EXCLUDE_KEYWORDS:
        if bad in blob:
            return False
    return True


def fetch_github_trending(session: Optional[requests.Session] = None) -> tuple[List[RepoItem], FetchReport]:
    """
    抓取过去 24 小时内"新建"或"有更新"的高星项目。

    对每个 topic 发两次检索：
      - created:>=<iso>  —— 24 小时内新建的项目
      - pushed:>=<iso>   —— 24 小时内有推送的项目（覆盖"更新"）
    结果按仓库去重，新建项目优先。
    """
    session = session or build_session()
    report = FetchReport(label="检索")

    # 提前校验 Token：与其让 8 次检索各自失败一次，不如一开始就说清楚问题
    if not config.GITHUB_TOKEN:
        raise GitHubAuthError(
            "缺少 GITHUB_TOKEN，无法调用 GitHub Search API。"
            "匿名配额（10 次/分钟）不足以稳定完成 8 次检索。"
        )

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=config.LOOKBACK_HOURS)
    iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(
        "开始抓取 GitHub 热门项目（%s 之后），topics=%s，已携带 Token 认证",
        iso,
        config.GITHUB_TOPICS,
    )

    merged: Dict[str, RepoItem] = {}

    for topic in config.GITHUB_TOPICS:
        for mode in ("created", "pushed"):
            # 有推送的项目池子很大，用更高的星标门槛保证质量
            star_floor = max(config.GITHUB_MIN_STARS, 200) if mode == "pushed" else config.GITHUB_MIN_STARS
            query = f"topic:{topic} {mode}:>={iso} stars:>={star_floor} fork:false archived:false"
            try:
                items = _search_repositories(session, query)
                report.ok += 1
            except GitHubAuthError:
                # 认证类错误是全局性问题，继续跑剩下的查询只会重复失败
                raise
            except Exception as exc:  # noqa: BLE001 —— 单个检索失败不应中断整体
                report.failed += 1
                message = f"GitHub 检索失败 [{topic}/{mode}]: {type(exc).__name__}: {exc}"
                report.add_error(message)
                logger.warning(message)
                continue

            for raw in items:
                try:
                    repo = _to_repo_item(raw, is_new=(mode == "created"))
                except (TypeError, ValueError, AttributeError) as exc:
                    # 单条脏数据不该拖垮整次采集
                    logger.debug("跳过格式异常的仓库条目：%s", exc)
                    continue

                if not _is_relevant_repo(repo):
                    continue

                existing = merged.get(repo.full_name)
                if existing is None:
                    merged[repo.full_name] = repo
                else:
                    # 同一仓库命中多次时：保留"新建"标记，并取更大的星数
                    existing.is_new = existing.is_new or repo.is_new
                    existing.stars = max(existing.stars, repo.stars)
                    if len(repo.description) > len(existing.description):
                        existing.description = repo.description

    repos = sorted(merged.values(), key=lambda r: (r.is_new, r.stars), reverse=True)
    repos = repos[: config.GITHUB_TOTAL_LIMIT]
    report.items = len(repos)

    if report.ok == 0:
        # 全部检索都失败：把失败原因显式抛出，让上层记成整体失败而不是"命中 0 条"
        raise RuntimeError(
            "GitHub 全部检索均失败：" + ("；".join(report.errors[:3]) or "原因未知")
        )

    logger.info("GitHub 采集完成：%s", report.summary)
    return repos, report


__all__ = [
    "NewsItem",
    "RepoItem",
    "FetchReport",
    "GitHubAuthError",
    "GitHubRateLimitError",
    "build_session",
    "clean_text",
    "fetch_all_news",
    "fetch_github_trending",
]
