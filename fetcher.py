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
import json
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
    readme: str = ""          # README 摘录，作为喂给模型的额外上下文
    homepage: str = ""

    def to_prompt_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
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
        if self.homepage:
            payload["homepage"] = self.homepage
        # README 可能很长，塞进 payload 前再兜一次长度，避免单条挤爆预算
        if self.readme:
            payload["readme_excerpt"] = self.readme
        return payload


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
    per_page: int = 0,
) -> List[Dict[str, Any]]:
    """
    调用一次 GitHub Search API。

    per_page 留空则用 config.GITHUB_PER_TOPIC_LIMIT。

    异常分级处理：
      - 401/403 且提示 token 无效 → GitHubAuthError，立即失败（重试无意义）
      - 403/429 限流               → 短退避后重试，最终抛出 GitHubRateLimitError
      - 5xx / 网络抖动             → 退避重试
      - 422 查询语法问题           → 直接抛错，不重试
    """
    headers = _github_headers()
    page_size = min(per_page or config.GITHUB_PER_TOPIC_LIMIT, 100)
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
                    "per_page": page_size,
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
        homepage=raw.get("homepage") or "",
    )


# --------------------------------------------------------------------------
# README 抓取（GitHub 板块的深度解析依赖它）
# --------------------------------------------------------------------------
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_BADGE_LINE_RE = re.compile(r"^\s*\[!\[.*$")
_MD_LINK_KEEP_TEXT_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_CODE_FENCE_RE = re.compile(r"^\s*```.*$", re.MULTILINE)
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_HR_RE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$", re.MULTILINE)


def strip_markdown(text: str) -> str:
    """
    把 README 压成适合喂模型的纯文本。

    README 里通常有大段徽章图片、居中 HTML 块、目录链接，这些对理解项目毫无帮助，
    还会白占 token 预算。这里把它们清掉，只留下真正描述项目的文字与命令。
    """
    if not text:
        return ""

    # 徽章行：典型形态是 [![CI](...)](...)，整行删掉
    lines = [ln for ln in text.splitlines() if not _MD_BADGE_LINE_RE.match(ln)]
    text = "\n".join(lines)

    text = _MD_IMAGE_RE.sub("", text)                 # 图片（含徽章）
    text = re.sub(r"<[^>]+>", " ", text)              # 内联 HTML
    text = _MD_LINK_KEEP_TEXT_RE.sub(r"\1", text)     # 链接只保留文字
    text = _MD_CODE_FENCE_RE.sub("", text)            # 代码围栏标记
    text = _MD_HEADING_RE.sub("", text)               # 标题井号
    text = _MD_HR_RE.sub("", text)                    # 分隔线
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def fetch_repo_readme(
    session: requests.Session,
    full_name: str,
    limit: int = 0,
) -> str:
    """
    抓取单个仓库的 README 摘录。

    GitHub 的 /readme 接口支持 Accept: application/vnd.github.raw 直接返回原文，
    省掉一次 base64 解码。取不到（无 README、限流、网络抖动）一律返回空字符串，
    让模型退回依赖自身的项目知识，绝不因为一个 README 失败而中断采集。
    """
    limit = limit or config.README_CHAR_LIMIT

    try:
        response = session.get(
            f"https://api.github.com/repos/{full_name}/readme",
            headers={**_github_headers(), "Accept": "application/vnd.github.raw"},
            timeout=config.HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.debug("README 请求异常 [%s]：%s", full_name, exc)
        return ""
    except GitHubAuthError:
        raise

    if response.status_code == 404:
        # 没有 README 是完全正常的情况，不值得记警告
        logger.debug("仓库无 README：%s", full_name)
        return ""

    if response.status_code == 403:
        logger.warning("README 抓取被限流 [%s]，本条将不带 README 上下文", full_name)
        return ""

    if response.status_code != 200:
        logger.debug("README 返回 HTTP %d [%s]", response.status_code, full_name)
        return ""

    cleaned = strip_markdown(response.text)
    if not cleaned:
        return ""

    if len(cleaned) > limit:
        # 在 limit 附近找一个换行，避免把句子从中间切断
        cut = cleaned.rfind("\n", 0, limit)
        cleaned = cleaned[: cut if cut > limit * 0.6 else limit].rstrip() + " …"

    return cleaned


def enrich_repos_with_readme(
    repos: List[RepoItem],
    session: Optional[requests.Session] = None,
    report: Optional[FetchReport] = None,
) -> int:
    """
    并发为仓库补充 README 摘录。返回成功取到 README 的仓库数。

    额外开销：每个仓库一次 core API 请求（配额 5000/小时，30 个仓库绰绰有余），
    与 Search API 的 30 次/分钟限额是两套独立配额，不会互相挤占。
    """
    if not repos:
        return 0

    session = session or build_session()
    targets = repos[: config.README_MAX_REPOS]
    got = 0

    logger.info("开始抓取 %d 个仓库的 README（每个上限 %d 字符）", len(targets), config.README_CHAR_LIMIT)

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {
            executor.submit(fetch_repo_readme, session, repo.full_name): repo for repo in targets
        }
        for future in as_completed(future_map):
            repo = future_map[future]
            try:
                text = future.result()
            except GitHubAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("README 抓取失败 [%s]：%s", repo.full_name, exc)
                if report is not None:
                    report.add_error(f"README [{repo.full_name}]: {type(exc).__name__}")
                continue

            if text:
                repo.readme = text
                got += 1

    logger.info("README 抓取完成：%d/%d 个仓库拿到有效内容", got, len(targets))
    return got


def _is_relevant_repo(repo: RepoItem) -> bool:
    """
    过滤掉明显与"效率 / AI 工具"主题无关的仓库。

    实测教训：只按 description 关键词过滤是不够的。`awesome-python`、
    `free-programming-books`、`public-apis` 这类精选清单星数极高、topics 又常打
    ai / productivity，会把整个板块占满。所以这里按两层拦：

      1. 仓库名正则 —— 名字里带 awesome- / -books / roadmap / tutorial 的，
         几乎必然是清单或教程，这是最可靠的信号
      2. 描述关键词 —— 兜住名字看不出、但描述暴露了的内容
    """
    if not repo.full_name or not repo.url:
        return False
    if repo.stars < config.GITHUB_MIN_STARS:
        return False

    name = repo.full_name.split("/")[-1].lower()
    for pattern in config.GITHUB_EXCLUDE_NAME_PATTERNS:
        if re.search(pattern, name):
            logger.debug("按名称规则排除：%s（命中 %s）", repo.full_name, pattern)
            return False

    blob = f"{repo.full_name} {repo.description}".lower()
    for bad in config.GITHUB_EXCLUDE_KEYWORDS:
        if bad in blob:
            logger.debug("按关键词排除：%s（命中 %s）", repo.full_name, bad)
            return False

    # 零 topic 的仓库多半是低投入或玩票项目（见 config 里的说明）
    if config.GITHUB_REQUIRE_TOPICS and not repo.topics:
        logger.debug("排除零 topic 仓库：%s", repo.full_name)
        return False

    return True


# --------------------------------------------------------------------------
# 轮播状态：记录「经典」项目推到第几名，以及最近推过哪些「趋势」项目
# --------------------------------------------------------------------------
def _empty_github_state() -> Dict[str, Any]:
    return {
        "classic_offset": 0,       # 经典池的下标游标，每次运行后 +GITHUB_CLASSIC_COUNT
        "classic_cycle": 0,        # 已完成的轮次，用于观测绕了几圈
        "classic_pool": [],        # 持久化的经典池（仓库名有序列表），保证排名稳定
        "classic_pool_built": "",  # 池子构建时间
        "recent_trending": [],     # 最近推过的趋势项目全名，用于次日去重
        "last_run_date": "",
        "last_classic": [],
        "updated_at": "",
    }


def load_github_state() -> Dict[str, Any]:
    """
    读取轮播状态。

    单文件损坏不应让整条流水线失败：解析不了就当作全新状态，从第 1 名重新开始。
    """
    path = config.GITHUB_STATE_FILE
    state = _empty_github_state()

    if not path.exists():
        logger.info("轮播状态文件不存在，本次从第 1 名开始：%s", path.name)
        return state

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("轮播状态文件损坏（%s），本次重置为初始状态：%s", path.name, exc)
        return state

    if not isinstance(data, dict):
        logger.warning("轮播状态文件结构异常，本次重置为初始状态")
        return state

    for key, default in state.items():
        value = data.get(key, default)
        # 类型不对就用默认值，避免脏数据把逻辑带偏
        if isinstance(default, list):
            state[key] = value if isinstance(value, list) else default
        elif isinstance(default, int):
            state[key] = value if isinstance(value, int) and value >= 0 else default
        else:
            state[key] = value if isinstance(value, str) else default

    logger.info(
        "轮播状态：经典池游标 = %d，已轮 %d 圈，记忆了 %d 个近期趋势项目",
        state["classic_offset"],
        state["classic_cycle"],
        len(state["recent_trending"]),
    )
    return state


def save_github_state(state: Dict[str, Any]) -> None:
    """
    写入轮播状态。

    这个文件放在 archive/ 下，会被工作流的「保存往期存档到仓库」步骤一起提交，
    所以不需要任何外部存储 —— git 就是我们的持久层。
    """
    path = config.GITHUB_STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "已保存轮播状态：下次经典池从第 %d 名开始（%s）",
            state["classic_offset"] + 1,
            path.name,
        )
    except OSError as exc:
        # 状态写不进去只会导致明天重复推荐，不该让整期早报失败
        logger.error("轮播状态写入失败（%s）：%s", path, exc)


# --------------------------------------------------------------------------
# 3 + 2 每日选品
# --------------------------------------------------------------------------
def _dedupe_repos(repos: List[RepoItem]) -> List[RepoItem]:
    """按仓库全名去重，保留星数更高的那条（同一仓库可能命中多个查询）。"""
    merged: Dict[str, RepoItem] = {}
    for repo in repos:
        key = repo.full_name.lower()
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = repo
        else:
            existing.is_new = existing.is_new or repo.is_new
            if repo.stars > existing.stars:
                existing.stars = repo.stars
            if len(repo.description) > len(existing.description):
                existing.description = repo.description
    return list(merged.values())


def select_trending_repos(
    session: requests.Session,
    state: Dict[str, Any],
) -> List[RepoItem]:
    """
    选 3 个「近期趋势」项目。

    重要限制：GitHub Search API **没有星标增长速度指标**，无法直接查"涨星最快"。
    这里用三个互补的查询来近似，共同点是都要求"项目本身是新的"：

      A. 创建于 7 天内            —— 本周刚冒头的新项目（最可靠的新鲜度信号）
      B. 创建于 7–30 天、星数达标  —— 本月内的新秀，补足候选量
      C. 30 天前创建、7 天内有推送、星数在区间内
                                  —— "近期翻红"，但有星数上限

    关于 C 的星数上限（这是踩过坑才加的）：
    最初用 `pushed:>=7d stars:>=3000`，结果每天都返回 freeCodeCamp(455k)、
    public-apis(480k) 这类仓库 —— 它们每天都在推送，所以"近期有推送"对它们永远成立。
    它们不是趋势，是永久霸榜的常驻民。加上星数上限后，C 才真正对应
    "中等规模、最近突然活跃起来"的项目。
    """
    now = datetime.now(timezone.utc)
    since_week = (now - timedelta(days=config.GITHUB_TREND_DAYS)).strftime("%Y-%m-%d")
    since_month = (now - timedelta(days=config.GITHUB_TREND_WIDER_DAYS)).strftime("%Y-%m-%d")
    old_cutoff = (
        now - timedelta(days=config.GITHUB_REVIVED_MIN_AGE_DAYS)
    ).strftime("%Y-%m-%d")

    seen_recent = {n.lower() for n in state.get("recent_trending", [])}

    # ⚠️ 关于 created 区间写法的坑（实测踩过，务必不要改回去）：
    #   created:>=A created:<B   → GitHub 会**静默忽略这两个限定符**，
    #                              返回一批 2013–2018 年的老仓库，且不报错
    #   created:A..B             → 正确生效
    #   created:A .. B（带空格）  → 返回 0 条
    # 所以这里一律用无空格的区间语法。
    queries = [
        (
            "本周新建",
            f"created:>={since_week} stars:>={config.GITHUB_TREND_STAR_FLOOR} "
            f"fork:false archived:false",
            True,
        ),
        (
            "本月新秀",
            f"created:{since_month}..{since_week} "
            f"stars:>={config.GITHUB_TREND_WIDER_STAR_FLOOR} fork:false archived:false",
            True,
        ),
        (
            "老将新动作",
            f"pushed:>={since_week} created:<{old_cutoff} "
            f"topic:{config.GITHUB_REVIVED_TOPIC} "
            f"stars:{config.GITHUB_REVIVED_STAR_FLOOR}..{config.GITHUB_REVIVED_STAR_CEILING} "
            f"fork:false archived:false",
            False,
        ),
    ]

    # 分桶收集：每个来源单独一个列表，后面按配额取，避免高星的「本月新秀」
    # 把真正最新鲜的「本周新建」全部挤掉（这是实测发现的问题）
    buckets: Dict[str, List[RepoItem]] = {}

    for label, query, is_new in queries:
        buckets[label] = []
        try:
            items = _search_repositories(session, query, per_page=40)
        except GitHubAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单个查询失败不该让整个板块空掉
            logger.warning("趋势查询失败 [%s]：%s", label, exc)
            continue

        for raw in items:
            try:
                repo = _to_repo_item(raw, is_new=is_new)
            except (TypeError, ValueError, AttributeError):
                continue
            if _is_relevant_repo(repo):
                buckets[label].append(repo)

        logger.info(
            "趋势查询 [%s] 命中 %d 个可用仓库（原始 %d 条）",
            label,
            len(buckets[label]),
            len(items),
        )

    # 去掉近期已推荐过的，并按星数排序（星数作为热度代理）
    for label in buckets:
        buckets[label] = sorted(
            (r for r in buckets[label] if r.full_name.lower() not in seen_recent),
            key=lambda r: r.stars,
            reverse=True,
        )

    # 配额轮转：三个来源各取一个，保证每天既有"刚出生的"也有"这个月的"和"老牌的"
    picked: List[RepoItem] = []
    taken: set = set()

    def take_one(label: str) -> bool:
        for repo in buckets.get(label, []):
            if repo.full_name in taken:
                continue
            taken.add(repo.full_name)
            picked.append(repo)
            return True
        return False

    labels = [label for label, _, _ in queries]
    for label in labels:
        if len(picked) >= config.GITHUB_TREND_COUNT:
            break
        take_one(label)

    # 某个来源今天没货（查询失败或窗口内确实没有）时，用其余来源补齐
    if len(picked) < config.GITHUB_TREND_COUNT:
        leftovers = sorted(
            (r for lst in buckets.values() for r in lst if r.full_name not in taken),
            key=lambda r: r.stars,
            reverse=True,
        )
        for repo in leftovers:
            if len(picked) >= config.GITHUB_TREND_COUNT:
                break
            taken.add(repo.full_name)
            picked.append(repo)

    # 仍然不够就回退用近期已推荐过的，保证板块不开天窗（会记警告便于排查）
    if len(picked) < config.GITHUB_TREND_COUNT:
        repeated = sorted(
            (
                r
                for lst in buckets.values()
                for r in lst
                if r.full_name.lower() in seen_recent and r.full_name not in taken
            ),
            key=lambda r: r.stars,
            reverse=True,
        )
        shortfall = config.GITHUB_TREND_COUNT - len(picked)
        picked.extend(repeated[:shortfall])
        if repeated:
            logger.warning(
                "趋势池新鲜项目不足，回退使用 %d 个近期已推荐过的仓库", min(shortfall, len(repeated))
            )

    logger.info(
        "趋势选品：%s → 选中 %s",
        {k: len(v) for k, v in buckets.items()},
        [f"{r.full_name}(★{r.stars})" for r in picked],
    )
    return picked


def _build_classic_pool(session: requests.Session) -> List[str]:
    """重建经典池：每个 topic 取星数最高的若干个，合并去重后按星数排序，只保留仓库名。"""
    pool: List[RepoItem] = []
    for topic in config.GITHUB_TOPICS:
        query = (
            f"topic:{topic} stars:>={config.GITHUB_CLASSIC_STAR_FLOOR} "
            f"fork:false archived:false"
        )
        try:
            items = _search_repositories(
                session, query, per_page=config.GITHUB_CLASSIC_POOL_PER_TOPIC
            )
        except GitHubAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("经典池查询失败 [%s]：%s", topic, exc)
            continue

        kept = 0
        for raw in items:
            try:
                repo = _to_repo_item(raw, is_new=False)
            except (TypeError, ValueError, AttributeError):
                continue
            if _is_relevant_repo(repo):
                pool.append(repo)
                kept += 1

        # topic 查空是个隐蔽的坑：曾经 tools-for-developers 这个 topic
        # 在 GitHub 上零仓库使用，导致池子凭空少了一大块却毫无提示
        if not items:
            logger.warning(
                "topic:%s 查询结果为空 —— 该 topic 可能不存在或已被弃用，"
                "建议换一个（验证方法：topic:<名字> stars:>=8000 能否查出东西）",
                topic,
            )
        else:
            logger.info("经典池 [%s] 收录 %d 个（原始 %d 条）", topic, kept, len(items))

    pool = _dedupe_repos(pool)
    pool.sort(key=lambda r: r.stars, reverse=True)
    return [r.full_name for r in pool]


def _fetch_repos_by_name(
    session: requests.Session, names: List[str]
) -> List[RepoItem]:
    """
    按仓库名逐个取最新元数据。

    经典池里只存了名字（保证排序稳定），星数/描述这些会变的信息在推送当天现取，
    避免把过期数据写进早报。
    """
    repos: List[RepoItem] = []
    for name in names:
        try:
            response = session.get(
                f"https://api.github.com/repos/{name}",
                headers=_github_headers(),
                timeout=config.HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("取仓库详情失败 [%s]：%s", name, exc)
            continue

        if response.status_code != 200:
            logger.warning("取仓库详情返回 HTTP %d [%s]", response.status_code, name)
            continue

        try:
            repos.append(_to_repo_item(response.json(), is_new=False))
        except (TypeError, ValueError, AttributeError) as exc:
            logger.warning("解析仓库详情失败 [%s]：%s", name, exc)

    return repos


def select_classic_repos(
    session: requests.Session,
    state: Dict[str, Any],
) -> List[RepoItem]:
    """
    选 2 个「历史经典」项目，按游标轮播推进。

    ⚠️ 池子必须**持久化排序**，不能每次运行重新按星数排。
    星数每天都在变，重排会让排名漂移 —— 今天排第 3 的明天可能变第 5，
    游标指向的仓库前后对不上，就会出现重复推荐（实测踩过这个坑）。
    所以：池子在首次构建或耗尽后重建，中间所有运行都复用同一份顺序，
    在推送当天再按名字取一次最新元数据。
    """
    need = config.GITHUB_CLASSIC_COUNT
    pool: List[str] = state.get("classic_pool") or []

    # ---- 首次运行、池子过期、或已轮完一圈：重建池子 ----
    stale = True
    built_at = state.get("classic_pool_built", "")
    if pool and built_at:
        try:
            age = (
                datetime.now(timezone.utc) - datetime.fromisoformat(built_at)
            ).days
            stale = age >= config.GITHUB_CLASSIC_POOL_MAX_AGE_DAYS
        except ValueError:
            stale = True

    if not pool or stale:
        reason = "首次构建" if not pool else f"池子已用满 {config.GITHUB_CLASSIC_POOL_MAX_AGE_DAYS} 天"
        logger.info("重建经典池（%s）", reason)
        rebuilt = _build_classic_pool(session)
        if rebuilt:
            pool = rebuilt
            state["classic_pool"] = pool
            state["classic_pool_built"] = datetime.now(timezone.utc).isoformat()
            state["classic_offset"] = 0
        elif not pool:
            logger.error("经典池构建失败且无历史池可用，本次不产出经典条目")
            return []

    if not pool:
        return []

    # ---- 按游标取，走到池尾就绕回开头 ----
    offset = state.get("classic_offset", 0)
    if offset >= len(pool):
        # 防御性兜底：正常情况下下面会在推完一圈时就把游标归零，走不到这里
        logger.warning("经典池游标 %d 越界（池大小 %d），归零重来", offset, len(pool))
        offset = 0

    picked_names = pool[offset : offset + need]
    next_offset = offset + need

    if next_offset >= len(pool):
        # 这一圈推完了，下一圈从头开始。
        # 关键：要在"推完的当下"就记圈并归零，不能等下次运行才发现越界 ——
        # 否则最后一组推完的那天不计数，轮次统计会永远少一圈（自检抓到过这个 off-by-one）。
        next_offset = 0
        state["classic_cycle"] = state.get("classic_cycle", 0) + 1
        logger.info(
            "经典池已推完一圈（共 %d 个），游标归零，累计完成 %d 圈",
            len(pool),
            state["classic_cycle"],
        )

    state["classic_offset"] = next_offset

    logger.info(
        "经典选品：池 %d 个，游标 %d → %d，选中 %s",
        len(pool),
        offset,
        next_offset,
        picked_names,
    )

    repos = _fetch_repos_by_name(session, picked_names)
    state["last_classic"] = [r.full_name for r in repos]
    return repos


def fetch_github_daily(
    session: Optional[requests.Session] = None,
) -> tuple[List[RepoItem], FetchReport]:
    """
    GitHub 板块的每日选品：3 个趋势 + 2 个经典 = 5 个。

    与旧逻辑的区别：不再一次抓 30 个候选让模型挑，而是**先按策略定好 5 个**，
    模型只负责把这 5 个讲清楚。好处是每天的推荐是确定性的、可复现的，
    而且提示词体积从 4.6 万字符降到几千，更快也更省。
    """
    session = session or build_session()
    report = FetchReport(label="检索")

    if not config.GITHUB_TOKEN:
        raise GitHubAuthError(
            "缺少 GITHUB_TOKEN，无法调用 GitHub Search API。"
            "匿名配额（10 次/分钟）不足以稳定完成多次检索。"
        )

    state = load_github_state()

    trending = select_trending_repos(session, state)
    classic = select_classic_repos(session, state)

    repos = trending + classic
    report.ok = 1
    report.items = len(repos)

    # ---- 更新状态：游标已由 select_classic_repos 推进，这里补上趋势记忆 ----
    memory: List[str] = list(state.get("recent_trending", []))
    for repo in trending:
        name = repo.full_name
        if name in memory:
            memory.remove(name)
        memory.append(name)
    state["recent_trending"] = memory[-config.GITHUB_RECENT_MEMORY :]
    state["last_run_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_github_state(state)

    # 补充 README 上下文：深度解析依赖它
    enrich_repos_with_readme(repos, session, report)

    logger.info(
        "GitHub 每日选品完成：趋势 %d + 经典 %d = %d 个项目",
        len(trending),
        len(classic),
        len(repos),
    )
    return repos, report



__all__ = [
    "NewsItem",
    "RepoItem",
    "FetchReport",
    "GitHubAuthError",
    "GitHubRateLimitError",
    "build_session",
    "clean_text",
    "strip_markdown",
    "fetch_all_news",
    "fetch_github_daily",
    "fetch_repo_readme",
    "enrich_repos_with_readme",
    "load_github_state",
    "save_github_state",
    "select_trending_repos",
    "select_classic_repos",
]
