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
import obsidian
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


def _payload_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _fit_budget_news(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    新闻部分的字符预算。

    新闻与 GitHub 现在是两次独立调用，各自有独立的上下文窗口，
    所以预算也分开分配，不再互相挤占。
    """
    budget = int(config.LLM_MAX_INPUT_CHARS * 0.6)
    news = payload["news"]

    while _payload_size(payload) > budget and news:
        news.pop()

    if news and _payload_size(payload) > budget:
        logger.warning("新闻素材仍超预算（%d > %d），截断摘要", _payload_size(payload), budget)
        for item in news:
            item["raw_summary"] = item.get("raw_summary", "")[:120]

    return payload


def _fit_budget_github(repos_payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    GitHub 部分的字符预算。

    README 摘录是这块的主要开销（每仓最多 1500 字符），超预算时按
    「砍仓库 → 缩短 README → 整段丢掉 README」三级退让，
    优先保证有足够多的候选进入模型，而不是让少数几个仓库独占预算。
    """
    budget = config.GITHUB_MAX_INPUT_CHARS
    original_count = len(repos_payload)

    while _payload_size(repos_payload) > budget and repos_payload:
        repos_payload.pop()

    dropped = original_count - len(repos_payload)
    if dropped:
        logger.warning(
            "GitHub 素材超预算（%d 字符），候选仓库由 %d 个裁剪为 %d 个",
            _payload_size(repos_payload),
            original_count,
            len(repos_payload),
        )

    if repos_payload and _payload_size(repos_payload) > budget:
        logger.warning("仍超预算，缩短 README 摘录至 600 字符")
        for repo in repos_payload:
            if repo.get("readme_excerpt"):
                repo["readme_excerpt"] = repo["readme_excerpt"][:600]

    if repos_payload and _payload_size(repos_payload) > budget:
        logger.warning("仍超预算，整体丢弃 README 上下文，退回依赖模型自身知识")
        for repo in repos_payload:
            repo.pop("readme_excerpt", None)

    return repos_payload


# --------------------------------------------------------------------------
# 提示词：新闻部分（四个资讯板块）
# --------------------------------------------------------------------------
NEWS_SYSTEM_PROMPT = """你是一名严谨的科技与消费行业资讯编辑，服务于一份每日早报。
你的唯一任务是把给定的原始资讯压缩、归类、重写成客观的中文简报。

你必须无条件遵守以下铁律：
1. 只能使用给定原始素材中的事实。禁止编造、脑补、外推任何事实、数字、公司名、人名或链接。
2. 客观陈述。禁止主观评论、价值判断、情绪化形容词、预测、投资建议和"值得关注"之类的话术。
3. 每条总结极度精简，控制在 60 个汉字以内，说清"谁 / 做了什么 / 关键结果或数据"即可。
4. 每条必须携带原始素材里真实存在的链接，原样复制，不得改写、拼接或臆造。
5. **双链标注**：总结里凡出现"有持续追踪价值的具名实体"，就用双方括号包起来，例如
   [[Anthropic]]、[[Claude]]、[[具身智能]]。要求：
   - 只标注具体的公司、产品、模型、技术名词、机构；泛化词（人工智能、手机、市场、
     芯片行业）一律不标 —— 标了会让双链图谱里塞满没有追踪价值的节点。
   - 不标注数字、日期、百分比。
   - **每条总结最多标注 3 个**；同一个实体只在其第一次出现处标注，不要反复刷屏。
   - 只出现在 summary 字段里，**绝对不要**写进 title（标题要保持干净、可直接检索）。
   - 括号内就是实体本名，不加"公司""概念""技术"之类后缀，也不要用 [[名称|别名]] 的竖线写法。
6. **关键词**：另外为当天整体提炼 3-5 个关键词（焦点公司名或核心概念），填入 keywords 字段。
   每个关键词是单个实体或概念，不带空格、标点、方括号、换行。
   优先从你在第 5 条里标注过的双链实体中挑选，这样 Obsidian 里的标签和双链能互相对应。
7. 只输出 JSON，不输出任何解释文字或 Markdown 代码块标记。"""

# 新闻板块（不含 github，GitHub 走独立提示词）
NEWS_SECTION_KEYS = ["intl_tech", "cn_tech", "intl_consumer", "cn_consumer"]
NEWS_SECTION_DEFS = [s for s in config.SECTION_DEFS if s["key"] in NEWS_SECTION_KEYS]


def _news_section_spec_text() -> str:
    return "\n".join(
        f"   {idx}) {sec['title']} —— JSON 键名 \"{sec['key']}\""
        for idx, sec in enumerate(NEWS_SECTION_DEFS, start=1)
    )


def build_news_user_prompt(payload: Dict[str, Any], report_date: date) -> str:
    date_str = report_date.strftime("%Y-%m-%d")
    weekday = _WEEKDAY_CN[report_date.weekday()]
    per_section = config.ITEMS_PER_SECTION

    schema_example = {
        "date": date_str,
        "digest": "一句话导读，40 字以内，概括当日最重要的 2-3 件事",
        "keywords": ["3-5 个关键词或焦点公司", "每个都是单个实体或概念", "不带空格与标点"],
        "sections": {
            "intl_tech": [
                {
                    "title": "中文标题，20 字以内，不要写双链",
                    "summary": "客观精简总结，60 字以内，具名实体用 [[双链]] 包裹",
                    "source": "来源媒体名",
                    "url": "https://原始素材中的真实链接",
                }
            ],
            "cn_tech": [],
            "intl_consumer": [],
            "cn_consumer": [],
        },
    }

    return f"""请把下面的原始素材整理成 {date_str}（{weekday}，Asia/Shanghai）的每日早报。

【板块要求】必须严格使用以下四个板块，不得增加、删除或改名：
{_news_section_spec_text()}

【分类口径】不设大公司专属板块。所有大厂动态——包括 AI、芯片、云服务、航天、自动驾驶、
具身智能、加密货币、算力基础设施等——一律归入对应的「国际科技新闻」或「国内科技新闻」，
判断依据是事件发生地与主体所在地。
「国际新消费新闻」「国内新消费新闻」聚焦零售、品牌、消费品、电商、冷链物流、餐饮、
服饰、美妆、出海消费、消费投融资等。

【数量】每个板块最多 {per_section} 条，按重要性从高到低排列。
原始素材不足以填满时，就给多少写多少，绝对禁止凑数或用无关内容填充。
素材明显不属于任何板块时直接丢弃。

【写作要求】
- 全部用中文输出。英文标题翻译成中文。
- 每条 summary 控制在 60 个汉字以内，一句话，主语明确，包含关键数字。
- 同一个事件被多家媒体报道时，只保留一条，选信息量最大的那家作为来源。
- source 字段填原始素材里的媒体名 / 站点名。
- digest 要覆盖当日最重要的 2-3 件事，不要只写一件事。
- summary 里的具名实体按系统提示的要求用 [[双链]] 包裹（每条最多 3 个），title 里不要出现双链。
- keywords 填 3-5 个当天整体的焦点公司或核心概念，用于给这篇笔记打标签。

【输出格式】只输出下面这个 JSON 对象，不要有任何前后缀文字：
{json.dumps(schema_example, ensure_ascii=False, indent=2)}

【原始素材】
{json.dumps(payload, ensure_ascii=False, indent=1)}
"""


# --------------------------------------------------------------------------
# 提示词：GitHub 板块（独立一套，目标是"让外行看懂"）
# --------------------------------------------------------------------------
GITHUB_SYSTEM_PROMPT = """你是一位擅长把开源项目讲给外行听的技术布道者，服务于一份面向普通读者的每日早报。
读者可能是产品经理、运营、设计师或刚入门的开发者——他们看不懂"基于 Rust 的异步运行时"这类表述。

你的任务是把开源项目翻译成人话。铁律：

1. **说人话。** 用日常语言解释它是什么、能帮人做什么。必须出现专业名词时，先用一句话把它解释清楚。
   反例：「基于 WASM 的边缘计算框架」  正例：「让网页跑得跟本地软件一样快的工具」。
2. **禁止编造。** 只依据给定的仓库元数据与 README 摘录。README 里没提到的功能不许写，
   不确定就说不知道，绝不允许为了把话说满而虚构。
3. **安装命令必须来自 README 原文。** README 里没有明确命令时，install 字段留空字符串，
   不要凭经验臆造 `npm install xxx` 这类命令——普通读者会照着敲，编造的命令会浪费他们的时间。
4. **不吹不黑。** 不写"革命性""颠覆性""必装"这类营销词，只陈述它实际做了什么。
5. **部署步骤不得臆造。** steps 里的每一步只允许是两类内容：README 里逐字出现的命令，
   或 README 里明确写出的前置条件（例如"需要先安装 Node 18"）。README 没写的一律不许补。
6. 只输出 JSON，不输出任何解释文字或 Markdown 代码块标记。"""


def build_github_user_prompt(repos_payload: List[Dict[str, Any]], report_date: date) -> str:
    date_str = report_date.strftime("%Y-%m-%d")

    schema_example = {
        "digest_repos": "一句话概括这批项目整体在解决什么问题，30 字以内",
        "repos": [
            {
                "repo": "owner/repo",
                "url": "https://github.com/owner/repo",
                "intro": "项目通俗简介：一句话说清它到底是个啥、解决什么痛点，40 字以内，必须是外行能懂的大白话",
                "features": [
                    "核心功能亮点 1，每条 20 字以内，说清能做什么而不是用了什么技术",
                    "核心功能亮点 2",
                    "核心功能亮点 3",
                ],
                "guide": "应用与部署指南：普通人该怎么用起来。1-2 句话，说清前置条件（要不要装 Node/Docker/Python）和大致步骤，80 字以内",
                "steps": [
                    "第 1 步：前置条件或第一条命令，25 字以内，逐字来自 README",
                    "第 2 步：下一条命令",
                ],
                "install": "从 README 原文摘出的安装或运行命令，单行；确实找不到任何命令才留空",
            }
        ],
    }

    with_readme = sum(1 for r in repos_payload if r.get("readme_excerpt"))
    without_readme = len(repos_payload) - with_readme

    # 让模型知道这 5 个是怎么选出来的，写导语时能呼应
    trending_names = [r["repo"] for r in repos_payload if r.get("is_new_in_24h")]

    return f"""今天是 {date_str}。下面是今天已经**预先选好**的 {len(repos_payload)} 个 GitHub 项目，
请把它们整理成早报的「GitHub 热门效率与 AI 工具」板块。

【选品说明】这 {len(repos_payload)} 个项目的挑选已经完成，**你不需要增删或替换**，只需逐个写成通俗解析。
它们由两部分组成：
- 其中 {len(trending_names)} 个是「近期趋势」新锐项目（近 7 天内新建或重新活跃）{("：" + "、".join(trending_names)) if trending_names else ""}
- 其余是「历史经典」项目（总星数排名靠前，按日轮播推荐）
写 digest_repos 导语时可以呼应这个结构，但不要生硬地贴标签。

【素材说明】
- 其中 {with_readme} 个项目附带了 `readme_excerpt`（README 原文摘录），这是你最主要的依据。
- 另有 {without_readme} 个项目没有 README 摘录，只能依据 description 和 topics 判断；
  信息不足时宁可写得保守，也不要编造功能。

【输出结构】每个项目必须完整包含以下四个部分，缺一不可：

1. **intro（项目通俗简介）** —— 一句话，40 字以内。
   必须回答"它到底是个啥 + 解决什么痛点"。用外行能懂的话，不要出现未解释的技术名词。

2. **features（核心功能亮点）** —— 2 到 4 条，每条 20 字以内。
   说清"能做什么"，而不是"用了什么技术"。不要罗列技术栈。
   优先挑对普通用户有感知的功能。README 里信息不足以支撑 2 条时就只写 1 条，不要凑数。

3. **guide（应用与部署指南）** —— 80 字以内，1-2 句话。
   要回答普通人最关心的：这东西怎么用起来？需要先装什么（Node / Docker / Python）？
   是在网页上用、本地跑、还是装成 App？有没有必须的前置条件（比如要申请 API Key）？

4. **install（一键命令）** —— 单行，**必须逐字来自 README**。
   README 里通常有 Installation / Quick Start / Getting Started / 安装 / 快速开始 这类章节，
   **请主动去定位并摘出其中最先出现的那条可执行命令**（安装或启动命令），不要因为没明说就跳过。
   只有确实通篇找不到任何可执行命令时（例如纯在线服务、纯文档项目）才留空字符串 ""。

5. **steps（部署步骤清单）** —— 2 到 5 条，每条 25 字以内，按"从零跑起来"的先后顺序排列。
   这是给读者一份照着做就能跑通的清单，所以要写成动宾短语或可直接复制执行的命令：
   - README 里有命令的，就把命令逐字写进来（例如 `npm install -g openclaw`）。
   - 没有命令的，写前置条件（例如「先安装 Docker Desktop」）。
   - 纯在线服务、打开网页就能用的，写访问方式（例如「访问官网注册后即可使用」）。
   - **不要自己编命令**；README 里确实什么都没有时，给空数组 []。
   - **不要在字符串里自带 `- [ ] `、`-`、`1.` 之类的前缀**，渲染时由程序统一添加，
     你自己加了会变成 `- [ ] - [ ] xxx` 这种重复前缀。

【覆盖要求】{len(repos_payload)} 个项目就要输出 {len(repos_payload)} 条，顺序与下面的素材保持一致。
只有在完全无法判断某个项目是做什么的（连 intro 都写不出来）时才允许省略，
并把省略的 repo 名字写进返回 JSON 的 "skipped" 数组里说明原因。

【digest_repos】另外用 30 字以内概括这批项目整体在解决什么问题，用于板块导语。

【输出格式】只输出下面这个 JSON 对象，不要有任何前后缀文字：
{json.dumps(schema_example, ensure_ascii=False, indent=2)}

【仓库素材】
{json.dumps(repos_payload, ensure_ascii=False, indent=1)}
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


def _chat_json(system_prompt: str, user_prompt: str, label: str) -> Dict[str, Any]:
    """
    调用 DeepSeek 并解析 JSON 输出，带指数退避重试。

    label 只用于日志区分（新闻 / GitHub 两次独立调用）。
    """
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

    logger.info(
        "调用 DeepSeek [%s]，模型=%s，提示词 %d 字符",
        label,
        config.DEEPSEEK_MODEL,
        len(user_prompt),
    )

    last_error: Optional[Exception] = None

    for attempt in range(1, config.LLM_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=config.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
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
                    "DeepSeek [%s] 返回成功（prompt=%s, completion=%s, total=%s）",
                    label,
                    getattr(usage, "prompt_tokens", "?"),
                    getattr(usage, "completion_tokens", "?"),
                    getattr(usage, "total_tokens", "?"),
                )
            return data

        except Exception as exc:  # noqa: BLE001
            last_error = exc
            wait = min(2 ** attempt, 30)
            logger.warning(
                "DeepSeek [%s] 第 %d/%d 次调用失败：%s",
                label,
                attempt,
                config.LLM_MAX_RETRIES,
                exc,
            )
            if attempt < config.LLM_MAX_RETRIES:
                time.sleep(wait)

    raise RuntimeError(
        f"DeepSeek [{label}] 调用在 {config.LLM_MAX_RETRIES} 次尝试后仍失败：{last_error}"
    )


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


def normalize_news_result(
    raw: Dict[str, Any],
    guard: UrlGuard,
    report_date: date,
) -> tuple[List[Dict[str, Any]], str, int, List[str]]:
    """
    规整新闻部分的模型输出。

    返回 (板块列表, 导读, 丢弃条数, 关键词)。
    板块顺序严格跟随 config.SECTION_DEFS 里的新闻板块。
    关键词是给 Obsidian 前置区打标签用的，会先经 obsidian.sanitize_tag 消毒。
    """
    digest = _coerce_str(raw.get("digest"), limit=120)
    keywords = obsidian.coerce_keywords(
        raw.get("keywords"), limit=config.OBSIDIAN_MAX_KEYWORDS
    )

    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, dict):
        raw_sections = {k: v for k, v in raw.items() if k in NEWS_SECTION_KEYS}

    sections: List[Dict[str, Any]] = []
    dropped = 0

    for sec_def in NEWS_SECTION_DEFS:
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

            items.append(
                {
                    "title": title,
                    "summary": summary,
                    "source": _coerce_str(entry.get("source"), limit=40)
                    or resolved.get("source", ""),
                    "url": resolved["url"],
                    # 原文对照字段：直接来自 RSS，未经模型改写
                    "title_original": title_original,
                    "excerpt": excerpt,
                    "is_foreign": is_foreign_text(title_original) or is_foreign_text(excerpt),
                }
            )

        sections.append({**sec_def, "items": items[: config.ITEMS_PER_SECTION]})

    return sections, digest, dropped, keywords


def _coerce_str_list(value: Any, limit: int = 60, max_items: int = 6) -> List[str]:
    """把模型返回的亮点列表规整成干净的字符串数组。"""
    if isinstance(value, str):
        # 模型偶尔会返回一整段用分号或换行分隔的文字
        parts = re.split(r"[\n;；]|(?<!\d)\.\s+", value)
    elif isinstance(value, list):
        parts = value
    else:
        return []

    result: List[str] = []
    for part in parts:
        text = _coerce_str(part, limit=limit)
        # 去掉模型可能加的项目符号前缀
        text = re.sub(r"^\s*[-*·•\d]+[.、)）]?\s*", "", text).strip()
        # 再去掉 Markdown 清单标记。模型（尤其写部署步骤时）经常自带 "- [ ] "，
        # 渲染 Obsidian 时我们还会再加一次前缀，不清掉就会变成 "- [ ] - [ ] xxx"。
        text = re.sub(r"^\[[ xX✓]?\]\s*", "", text).strip()
        if text:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


def normalize_github_result(
    raw: Dict[str, Any],
    repos: List[RepoItem],
    guard: UrlGuard,
) -> tuple[List[Dict[str, Any]], str, int]:
    """
    规整 GitHub 板块的模型输出。

    与新闻板块的关键差异：
      - stars / language / 仓库名一律以采集到的原始数据为准，不采用模型填的值
        （模型很容易把星数写错，而这是可以零成本取到准确值的字段）
      - 输出的是结构化字段（intro / features / guide / install），而不是一段 summary
    """
    repos_by_name = {r.full_name.lower(): r for r in repos}
    repos_by_url = {normalize_url(r.url): r for r in repos}

    raw_repos = raw.get("repos")
    if not isinstance(raw_repos, list):
        # 容错：模型有时直接把数组放在顶层
        raw_repos = raw if isinstance(raw, list) else []

    intros: List[str] = []
    items: List[Dict[str, Any]] = []
    dropped = 0

    for entry in raw_repos:
        if not isinstance(entry, dict):
            continue

        name = _coerce_str(entry.get("repo") or entry.get("title"), limit=120)
        url = _coerce_str(entry.get("url"), limit=500)

        # 先按仓库名精确匹配，再退回链接校验
        repo = repos_by_name.get(name.lower()) or repos_by_url.get(normalize_url(url))
        if repo is None:
            resolved = guard.resolve(url, name)
            if resolved is None:
                dropped += 1
                logger.warning("丢弃无法验证的仓库条目：%s", name or url)
                continue
            repo = repos_by_url.get(normalize_url(resolved["url"]))
            if repo is None:
                dropped += 1
                logger.warning("仓库条目链接对不上原始素材：%s", resolved["url"])
                continue

        intro = _coerce_str(entry.get("intro") or entry.get("summary"), limit=160)
        features = _coerce_str_list(entry.get("features"), limit=60, max_items=5)
        guide = _coerce_str(entry.get("guide"), limit=260)
        install = _coerce_str(entry.get("install"), limit=200)
        # 部署步骤：给 Obsidian 渲染成 "- [ ] " 待办清单用。
        # 空数组是合法结果（README 里确实没有任何可执行命令的项目）。
        steps = _coerce_str_list(entry.get("steps"), limit=140, max_items=6)

        if not intro and not features:
            # 连简介和亮点都没有，这条没有展示价值
            dropped += 1
            continue

        # 降级兜底：模型没给简介时用仓库原始 description，而不是留空
        if not intro:
            intro = _coerce_str(repo.description, limit=160)

        items.append(
            {
                "title": repo.full_name,
                "url": repo.url,
                # 结构化四件套
                "intro": intro,
                "features": features,
                "guide": guide,
                "steps": steps,
                "install": install,
                # summary 与 intro 保持一致，让邮件/纯文本等旧通道无需改动即可复用
                "summary": intro,
                "source": "GitHub",
                # 星数与语言取采集到的真实值
                "stars": str(repo.stars) if repo.stars else "",
                "language": repo.language,
                "is_new": repo.is_new,
                "title_original": "",
                "excerpt": repo.description,
                "is_foreign": True,
            }
        )

        if intro:
            intros.append(intro)

    if dropped:
        logger.warning("GitHub 板块共丢弃 %d 条无法验证的条目", dropped)

    items = items[: config.GITHUB_DISPLAY_COUNT]
    lead = _coerce_str(raw.get("digest_repos"), limit=80)

    return items, lead, dropped


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
            "url": repo.url,
            # 降级时没有 AI 解析，四个结构化字段留空，页面只展示仓库描述
            "intro": repo.description or "（AI 解析不可用，仅展示仓库原始描述）",
            "features": [],
            "guide": "",
            "steps": [],
            "install": "",
            "summary": repo.description or "（AI 解析不可用，仅展示仓库原始描述）",
            "source": "GitHub",
            "stars": str(repo.stars),
            "language": repo.language,
            "is_new": repo.is_new,
            "title_original": "",
            "excerpt": "",
            "is_foreign": True,
            "untranslated": True,
        }
        for repo in repos[: config.GITHUB_DISPLAY_COUNT]
    ]

    sections = [
        {**sec_def, "items": section_items.get(sec_def["key"], [])}
        for sec_def in config.SECTION_DEFS
    ]

    return {
        "date": report_date.isoformat(),
        "digest": f"AI 摘要暂不可用（{reason}），以下为原始采集内容。",
        "keywords": [],
        "sections": sections,
        "degraded": True,
        "degraded_reason": reason,
    }


def build_fallback_news_section(
    news: List[NewsItem], reason: str
) -> tuple[List[Dict[str, Any]], int]:
    """只降级新闻部分，保留 GitHub 板块的 AI 结果。"""
    buckets: Dict[Tuple[str, str], List[NewsItem]] = {b: [] for b in _BUCKETS}
    for item in news:
        key = (item.region or "", item.category or "")
        if key in buckets:
            buckets[key].append(item)

    sections: List[Dict[str, Any]] = []
    for sec_def in NEWS_SECTION_DEFS:
        bucket = {"intl_tech": ("intl", "tech"), "cn_tech": ("cn", "tech"),
                  "intl_consumer": ("intl", "consumer"), "cn_consumer": ("cn", "consumer")}[
            sec_def["key"]
        ]
        items = [
            {
                "title": item.title,
                "summary": item.summary or f"（AI 摘要不可用：{reason}）",
                "source": item.source,
                "url": item.url,
                "title_original": "",
                "excerpt": "",
                "is_foreign": is_foreign_text(item.title),
                "untranslated": True,
            }
            for item in buckets[bucket][: config.ITEMS_PER_SECTION]
        ]
        sections.append({**sec_def, "items": items})

    return sections, len(sections)


def build_fallback_github_section(repos: List[RepoItem]) -> List[Dict[str, Any]]:
    """只降级 GitHub 板块。"""
    return [
        {
            "title": repo.full_name,
            "url": repo.url,
            "intro": repo.description or "（AI 解析不可用，仅展示仓库原始描述）",
            "features": [],
            "guide": "",
            "steps": [],
            "install": "",
            "summary": repo.description or "（AI 解析不可用，仅展示仓库原始描述）",
            "source": "GitHub",
            "stars": str(repo.stars),
            "language": repo.language,
            "is_new": repo.is_new,
            "title_original": "",
            "excerpt": "",
            "is_foreign": True,
            "untranslated": True,
        }
        for repo in repos[: config.GITHUB_DISPLAY_COUNT]
    ]


# --------------------------------------------------------------------------
# 对外主入口
# --------------------------------------------------------------------------
def summarize(
    news: List[NewsItem],
    repos: List[RepoItem],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    完整提炼流程。

    新闻与 GitHub 走两次独立的 DeepSeek 调用：
      - 两边的素材形态、写作要求、输出结构完全不同，拆开可以让提示词各司其职
      - 任一侧失败只降级那一侧，不会因为 GitHub README 拉垮而丢掉全部新闻
    整体仍然保证不抛异常：最坏情况退回全量降级结果。
    """
    report_date = today_local()
    guard = UrlGuard(news, repos)

    if dry_run:
        return build_fallback_result(news, repos, report_date, "dry-run 模式")

    candidates = select_candidates(news)
    logger.info("送入模型的新闻候选 %d 条 / 仓库候选 %d 个", len(candidates), len(repos))

    news_payload = _fit_budget_news(
        {
            "report_date": report_date.isoformat(),
            "news": [item.to_prompt_dict() for item in candidates],
        }
    )
    github_payload = _fit_budget_github([repo.to_prompt_dict() for repo in repos])

    if not news_payload["news"] and not github_payload:
        return build_fallback_result(news, repos, report_date, "无可用原始素材")

    degraded_parts: List[str] = []
    news_sections: List[Dict[str, Any]] = []
    github_items: List[Dict[str, Any]] = []
    github_lead = ""
    digest = ""
    keywords: List[str] = []

    # ---- 新闻部分 ----
    if news_payload["news"]:
        try:
            raw = _chat_json(
                NEWS_SYSTEM_PROMPT,
                build_news_user_prompt(news_payload, report_date),
                "新闻",
            )
            news_sections, digest, _, keywords = normalize_news_result(raw, guard, report_date)
        except Exception as exc:  # noqa: BLE001
            logger.error("新闻板块提炼失败：%s", exc, exc_info=True)
            news_sections, _ = build_fallback_news_section(news, f"{type(exc).__name__}: {exc}")
            degraded_parts.append(f"新闻（{type(exc).__name__}）")
    else:
        news_sections, _ = build_fallback_news_section(news, "无候选素材")
        degraded_parts.append("新闻（无候选素材）")

    # ---- GitHub 部分 ----
    if github_payload:
        enriched = sum(1 for r in github_payload if r.get("readme_excerpt"))
        logger.info(
            "GitHub 板块：%d 个仓库（其中 %d 个带 README 上下文）", len(github_payload), enriched
        )
        try:
            raw = _chat_json(
                GITHUB_SYSTEM_PROMPT,
                build_github_user_prompt(github_payload, report_date),
                "GitHub",
            )
            github_items, github_lead, _ = normalize_github_result(raw, repos, guard)
        except Exception as exc:  # noqa: BLE001
            logger.error("GitHub 板块提炼失败：%s", exc, exc_info=True)
            github_items = build_fallback_github_section(repos)
            degraded_parts.append(f"GitHub（{type(exc).__name__}）")
    else:
        github_items = build_fallback_github_section([])

    # ---- 合并 ----
    github_def = next(s for s in config.SECTION_DEFS if s["key"] == "github")
    github_section = {**github_def, "items": github_items, "lead": github_lead}

    sections = news_sections + [github_section]

    total = sum(len(sec["items"]) for sec in sections)
    if total == 0:
        return build_fallback_result(news, repos, report_date, "所有条目都未通过链接校验")

    if not digest:
        digest = github_lead or "今日科技与消费要闻速览。"

    result: Dict[str, Any] = {
        "date": report_date.isoformat(),
        "digest": digest,
        # 当天整体的焦点公司 / 核心概念，Obsidian 前置区用它拼 tags
        "keywords": keywords,
        "sections": sections,
    }

    if degraded_parts:
        # 部分降级：页面顶部提示哪一块没走通，但整体仍然是 AI 输出
        result["degraded"] = True
        result["degraded_reason"] = "以下板块未完成 AI 处理：" + "、".join(degraded_parts)
        result["partial_degraded"] = True

    logger.info(
        "AI 提炼完成，共 %d 条内容（新闻 %d / GitHub %d）%s",
        total,
        sum(len(s["items"]) for s in news_sections),
        len(github_items),
        "；部分降级：" + "、".join(degraded_parts) if degraded_parts else "",
    )
    return result


__all__ = [
    "summarize",
    "today_local",
    "format_date_cn",
    "local_now",
    "normalize_url",
    "UrlGuard",
    "build_fallback_result",
    "build_fallback_news_section",
    "build_fallback_github_section",
    "normalize_news_result",
    "normalize_github_result",
]
