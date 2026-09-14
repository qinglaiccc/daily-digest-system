# -*- coding: utf-8 -*-
"""
全局配置中心。

设计原则：
1. 所有"会变的东西"集中在这里（信息源、板块定义、模型参数、路径、超时）。
2. 密钥一律从环境变量读取，绝不硬编码，方便在 GitHub Actions 里用 Secrets 注入。
3. 模块内不 import 其他业务模块，避免循环依赖。
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "template.html"
TEMPLATE_INDEX_PATH = BASE_DIR / "template_index.html"
DIST_DIR = BASE_DIR / "dist"           # 部署目录（gh-pages 只发布这个目录）
OUTPUT_HTML = DIST_DIR / "index.html"
RAW_DUMP_PATH = DIST_DIR / "_raw.json"  # 调试用：原始采集结果快照

# 往期存档：按天一份 JSON，提交回仓库，是历史记录的唯一数据源
ARCHIVE_DIR = BASE_DIR / "archive"
HISTORY_MANIFEST = DIST_DIR / "history.json"


# --------------------------------------------------------------------------
# .env 加载（本地开发用；Actions 上走 Secrets，不会读这个文件）
# --------------------------------------------------------------------------
def load_dotenv(path: Path | None = None) -> int:
    """
    极简 .env 解析：KEY=VALUE，支持 # 注释与引号。已存在的环境变量不覆盖。

    自己实现是为了不引入 python-dotenv 依赖，逻辑只有二十来行。
    """
    path = path or (BASE_DIR / ".env")
    if not path.exists():
        return 0

    loaded = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key:
            continue

        # 不覆盖已经存在的环境变量，保证 Actions Secrets 优先级最高
        if key not in os.environ or not os.environ[key]:
            os.environ[key] = value
            loaded += 1

    return loaded


load_dotenv()

# --------------------------------------------------------------------------
# 时区 / 日期
# --------------------------------------------------------------------------
TIMEZONE = os.getenv("REPORT_TIMEZONE", "Asia/Shanghai")
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))   # 只取过去 N 小时的内容
MAX_ITEM_AGE_HOURS = int(os.getenv("MAX_ITEM_AGE_HOURS", "36"))  # 无时间戳时的兜底上限

# --------------------------------------------------------------------------
# 密钥（GitHub Secrets 注入）
# --------------------------------------------------------------------------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()

# 站点地址：留空则自动从 GITHUB_REPOSITORY 推导
SITE_URL = os.getenv("SITE_URL", "").strip()

# --------------------------------------------------------------------------
# 邮件推送（SMTP，使用 Python 标准库 smtplib + email.mime，无第三方依赖）
# --------------------------------------------------------------------------
SMTP_HOST = (os.getenv("SMTP_HOST") or "smtp.qq.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT") or "465")
SMTP_USER = os.getenv("SMTP_USER", "").strip()
# QQ 邮箱这里填的是「授权码」，不是登录密码
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
# 收件人；留空则发给自己（收发同址）
RECEIVER_EMAIL = (os.getenv("RECEIVER_EMAIL") or SMTP_USER).strip()
# 可选：收件人列表，逗号分隔。设置后覆盖 RECEIVER_EMAIL
RECEIVER_EMAILS_EXTRA = os.getenv("RECEIVER_EMAILS_EXTRA", "").strip()
# 发件人显示名
SMTP_FROM_NAME = (os.getenv("SMTP_FROM_NAME") or "每日早报").strip()
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "30"))
# 465 走 SSL（QQ 邮箱默认），587 走 STARTTLS
SMTP_USE_SSL = (os.getenv("SMTP_USE_SSL") or ("true" if SMTP_PORT == 465 else "false")).lower() in (
    "1",
    "true",
    "yes",
)

# --------------------------------------------------------------------------
# DeepSeek 调用参数
# --------------------------------------------------------------------------
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "240"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_MAX_INPUT_CHARS = int(os.getenv("LLM_MAX_INPUT_CHARS", "45000"))  # 原始素材字符预算

ITEMS_PER_SECTION = int(os.getenv("ITEMS_PER_SECTION", "8"))   # 每板块目标条数
CANDIDATES_PER_SECTION = int(os.getenv("CANDIDATES_PER_SECTION", "22"))  # 送进模型的候选条数

# 往期：保留并渲染多少天的历史
HISTORY_MAX_DAYS = int(os.getenv("HISTORY_MAX_DAYS", "30"))

# --------------------------------------------------------------------------
# 网络参数
# --------------------------------------------------------------------------
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "2"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    # 用常见的浏览器 UA：不少站点的 WAF 会直接拒绝带 "bot" 字样的请求，
    # 导致拿到 HTML 拦截页而不是 feed。RSS 本质上是公开订阅接口，这里只是取回订阅内容。
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)

# --------------------------------------------------------------------------
# 板块定义
#
# 说明：Swiss 风格只允许一个点缀色，所以这里不再为每个板块分配颜色，
# 改用编号（01–05）做视觉区分，全站不依赖任何 emoji 字形。
# --------------------------------------------------------------------------
SECTION_DEFS = [
    {
        "key": "intl_tech",
        "num": "01",
        "title": "国际科技新闻",
        "title_en": "International Tech",
        "desc": "全球科技产业动态",
    },
    {
        "key": "cn_tech",
        "num": "02",
        "title": "国内科技新闻",
        "title_en": "China Tech",
        "desc": "中国科技产业动态",
    },
    {
        "key": "intl_consumer",
        "num": "03",
        "title": "国际新消费新闻",
        "title_en": "International Consumer",
        "desc": "全球零售与消费品牌",
    },
    {
        "key": "cn_consumer",
        "num": "04",
        "title": "国内新消费新闻",
        "title_en": "China Consumer",
        "desc": "中国零售与新消费品牌",
    },
    {
        "key": "github",
        "num": "05",
        "title": "GitHub 热门效率与 AI 工具",
        "title_en": "GitHub Trending Tools",
        "desc": "过去 24 小时活跃的开源项目",
    },
]

SECTION_KEYS = [s["key"] for s in SECTION_DEFS]
SECTION_TITLE_MAP = {s["key"]: s["title"] for s in SECTION_DEFS}

# --------------------------------------------------------------------------
# RSS 信息源
#
# region  : intl / cn   —— 只作为给模型的线索，最终归属由模型按内容判断
# category: tech / consumer —— 同上，仅作线索
# enabled : False 表示该源当前不可用，保留配置便于日后恢复
#
# 维护提示：下面的 enabled=True 的源都经过实际连通性验证。部分中文站点已下线官方 RSS
# （36氪、机器之心、品玩、联商网、亿邦动力等返回 HTML 或 404），因此替换成了同类可用源。
# 若某个源长期失败，把 enabled 改成 False，并在 README「信息源维护」一节按格式补新源。
# --------------------------------------------------------------------------
RSS_SOURCES = [
    # ================= 国际科技 =================
    {
        "name": "TechCrunch",
        "url": "https://techcrunch.com/feed/",
        "region": "intl",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "The Verge",
        "url": "https://www.theverge.com/rss/index.xml",
        "region": "intl",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "Ars Technica",
        "url": "https://feeds.arstechnica.com/arstechnica/index",
        "region": "intl",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "Engadget",
        "url": "https://www.engadget.com/rss.xml",
        "region": "intl",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "Wired",
        "url": "https://www.wired.com/feed/rss",
        "region": "intl",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "Hacker News",
        "url": "https://hnrss.org/frontpage",
        "region": "intl",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "MIT Technology Review",
        "url": "https://www.technologyreview.com/feed/",
        "region": "intl",
        "category": "tech",
        "enabled": True,
    },

    # ================= 国内科技 =================
    {
        "name": "IT之家",
        "url": "https://www.ithome.com/rss/",
        "region": "cn",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "量子位",
        "url": "https://www.qbitai.com/feed",
        "region": "cn",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "极客公园",
        "url": "https://www.geekpark.net/rss",
        "region": "cn",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "钛媒体",
        "url": "https://www.tmtpost.com/rss.xml",
        "region": "cn",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "Solidot",
        "url": "https://www.solidot.org/index.rss",
        "region": "cn",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "少数派",
        "url": "https://sspai.com/feed",
        "region": "cn",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "InfoQ 中文",
        "url": "https://www.infoq.cn/feed",
        "region": "cn",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "爱范儿",
        "url": "https://www.ifanr.com/feed",
        "region": "cn",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "199IT",
        "url": "https://www.199it.com/feed",
        "region": "cn",
        "category": "tech",
        "enabled": True,
    },

    # ================= 国际新消费 =================
    # Industry Dive 系列（零售/餐饮/食品/营销），每日多更
    {
        "name": "Retail Dive",
        "url": "https://www.retaildive.com/feeds/news/",
        "region": "intl",
        "category": "consumer",
        "enabled": True,
    },
    {
        "name": "Grocery Dive",
        "url": "https://www.grocerydive.com/feeds/news/",
        "region": "intl",
        "category": "consumer",
        "enabled": True,
    },
    {
        "name": "Fashion Dive",
        "url": "https://www.fashiondive.com/feeds/news/",
        "region": "intl",
        "category": "consumer",
        "enabled": True,
    },
    {
        "name": "Food Dive",
        "url": "https://www.fooddive.com/feeds/news/",
        "region": "intl",
        "category": "consumer",
        "enabled": True,
    },
    {
        "name": "Marketing Dive",
        "url": "https://www.marketingdive.com/feeds/news/",
        "region": "intl",
        "category": "consumer",
        "enabled": True,
    },
    {
        "name": "Modern Retail",
        "url": "https://www.modernretail.co/feed/",
        "region": "intl",
        "category": "consumer",
        "enabled": True,
    },
    # 以下源在部分网络环境下会被重置连接，如在 Actions 中验证可用可打开
    {
        "name": "Retail Brew",
        "url": "https://www.retailbrew.com/feed",
        "region": "intl",
        "category": "consumer",
        "enabled": False,
    },
    {
        "name": "PYMNTS",
        "url": "https://www.pymnts.com/feed/",
        "region": "intl",
        "category": "consumer",
        "enabled": False,
    },
    {
        "name": "The Drum",
        "url": "https://www.thedrum.com/feeds/all",
        "region": "intl",
        "category": "consumer",
        "enabled": False,
    },

    # ================= 国内新消费 =================
    # 说明：国内消费垂类的官方 RSS 绝大多数已下线，这里只保留实测可用的源。
    # 由于板块归属由模型按内容判断，国内科技源里的消费类新闻同样会被归入本板块。
    {
        "name": "刀法研究所",
        "url": "https://www.digitaling.com/rss",
        "region": "cn",
        "category": "consumer",
        "enabled": True,
    },
    {
        "name": "中新网财经",
        "url": "https://www.chinanews.com.cn/rss/finance.xml",
        "region": "cn",
        "category": "consumer",
        "enabled": True,
    },
    # 36氪官方 RSS 已下线（返回 HTML 页面），改用 RSSHub 路由；需自建或使用可达实例
    {
        "name": "36氪",
        "url": "https://rsshub.app/36kr/newsflashes",
        "region": "cn",
        "category": "consumer",
        "enabled": False,
    },
]

# --------------------------------------------------------------------------
# GitHub 热门项目采集
# --------------------------------------------------------------------------
GITHUB_TOPICS = [
    "ai",
    "machine-learning",
    "productivity",
    "tools-for-developers",
]

GITHUB_MIN_STARS = int(os.getenv("GITHUB_MIN_STARS", "50"))
GITHUB_PER_TOPIC_LIMIT = int(os.getenv("GITHUB_PER_TOPIC_LIMIT", "15"))
GITHUB_TOTAL_LIMIT = int(os.getenv("GITHUB_TOTAL_LIMIT", "30"))

# README 深度解析：抓取每个仓库 README 的字符上限，以及最多抓多少个仓库
# 注意 /readme 走的是 core 配额（5000/小时），与 Search 的 30 次/分钟是两套独立配额
README_CHAR_LIMIT = int(os.getenv("README_CHAR_LIMIT", "1500"))
README_MAX_REPOS = int(os.getenv("README_MAX_REPOS", "30"))

# GitHub 板块是独立的一次 DeepSeek 调用，上下文预算单独给。
# README 摘录很占空间（1500 字符/个），预算给足才能让更多仓库带着上下文进模型。
# 45000 字符约合 15k tokens，deepseek-chat 的上下文完全放得下。
GITHUB_MAX_INPUT_CHARS = int(os.getenv("GITHUB_MAX_INPUT_CHARS", "45000"))

# 明显偏离"效率与 AI 工具"主题的仓库会被关键词过滤掉
GITHUB_EXCLUDE_KEYWORDS = [
    "awesome-list-of-awesome-lists",
    "interview-questions",
    "leetcode-solutions",
    "dotfiles",
    "wallpaper",
    "cheatsheet-collection",
]

# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------
NOTIFY_ENABLED = os.getenv("NOTIFY_ENABLED", "true").lower() not in ("0", "false", "no")
EMAIL_DIGEST_ITEMS = int(os.getenv("EMAIL_DIGEST_ITEMS", "6"))   # 邮件里列出的导读条数
EMAIL_SUBJECT_MAX = 80                                            # 主题长度上限（字符）


def resolve_recipients() -> list[str]:
    """
    收件人列表：RECEIVER_EMAIL 打底，RECEIVER_EMAILS_EXTRA 以逗号分隔追加。

    去重且保持顺序，方便「发给自己 + 顺便抄送几个同事」的场景。
    """
    candidates: list[str] = []

    if RECEIVER_EMAIL:
        candidates.append(RECEIVER_EMAIL)

    if RECEIVER_EMAILS_EXTRA:
        candidates.extend(RECEIVER_EMAILS_EXTRA.replace(";", ",").split(","))

    seen: set[str] = set()
    result: list[str] = []
    for addr in candidates:
        addr = addr.strip()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        result.append(addr)

    return result


def resolve_site_url() -> str:
    """
    推导部署后的网页地址。

    优先级：SITE_URL 环境变量 > GitHub Actions 提供的仓库信息 > 本地占位符。
    """
    if SITE_URL:
        return SITE_URL.rstrip("/") + "/"

    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        # 用户主页仓库（owner.github.io）的地址形式不同
        if name.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner}.github.io/"
        return f"https://{owner}.github.io/{name}/"

    return "https://example.github.io/daily-report/"


def pages_url_for_log() -> str:
    return resolve_site_url()
