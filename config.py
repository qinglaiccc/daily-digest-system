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

# Obsidian 知识库导出：按天一份 Markdown，同样提交回仓库
OBSIDIAN_DIR = BASE_DIR / "obsidian"


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
        # 【2026-09 停用】实测依据：Wired 原始抓取 13 条看似干净，但它进入最终
        # 早报的 3 条**全部**是政治/监控类（美中期选举、国防部 UAP、Flock 车牌摄像头），
        # 对"科技产业动态"板块是 0 命中。它的选题重心偏政策与社会议题，不适合本板块。
        "enabled": False,
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
        # 【2026-09 停用】与 Fashion Dive / Food Dive / Marketing Dive 同批处理：
        # 这四个英文消费垂类源在最近一期里合计贡献 0 条入选内容（国际新消费板块整体 0 条）。
        # 国际消费板块改由 Retail Dive + Modern Retail 两个源支撑。
        "enabled": False,
    },
    {
        "name": "Fashion Dive",
        "url": "https://www.fashiondive.com/feeds/news/",
        "region": "intl",
        "category": "consumer",
        "enabled": False,
    },
    {
        "name": "Food Dive",
        "url": "https://www.fooddive.com/feeds/news/",
        "region": "intl",
        "category": "consumer",
        "enabled": False,
    },
    {
        "name": "Marketing Dive",
        "url": "https://www.marketingdive.com/feeds/news/",
        "region": "intl",
        "category": "consumer",
        "enabled": False,
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
        # 【2026-09 停用】实测依据：这是本次换血里证据最硬的一条 —— 它的原始抓取有 30 条、
        # 看似干净，但进入最终早报的 5 条**全部**不是消费新闻：美联储加息、A股粮食板块、
        # 中国汽车全球化、扬州港风电出口、海南华龙一号核电。命中率 0/5。
        # 根因不是它质量差，而是**它的栏目定位是宏观财经与产业经济，被错标成了 consumer**。
        # 已由 ranking.py 的"板块相关性闸门"兜底，但既然它 100% 不匹配本板块，直接停用更省事。
        "enabled": False,
    },

    # ================= 新增：前沿极客与 AI 专属流（2026-09 实测接入）=================
    # 说明：以下每个源都经过「用项目自己的采集规则跑一遍」的实测，
    # 只有能稳定返回、且条目时间戳在窗口内可用的才被启用。
    # ⚠️ 被实测排除、**没有**接入的候选源（避免以后有人重复劳动）：
    #   · TLDR AI (tldr.tech/api/rss/ai)：条目是一整期 newsletter 的标题拼接
    #     （"Muse connectors, Meta SAM 3.1, Gemini hacks companies"），正文为空、
    #     链接指向期号页面而非单条新闻 —— 无法拆成单条资讯，接入会污染数据。
    #   · arXiv cs.AI (rss.arxiv.org/rss/cs.AI)：每天 214 条论文标题的洪流，
    #     会把候选池彻底淹没。论文维度已由 HuggingFace Daily Papers（社区投票的每日热门）
    #     覆盖，那是有筛选的，质量远高于原始 arXiv 流。
    #   · Anthropic News (anthropic.com/news/rss.xml)：该 RSS 不存在，返回空 feed。
    #   · GitHub Blog / Google DeepMind Blog：更新太慢，24 小时内 0 条。
    {
        "name": "Hacker News 高分",
        "url": "https://hnrss.org/best",
        "region": "intl",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "Lobsters",
        "url": "https://lobste.rs/rss",
        "region": "intl",
        "category": "tech",
        "enabled": True,
    },
    {
        "name": "Simon Willison",
        "url": "https://simonwillison.net/atom/everything/",
        "region": "intl",
        "category": "tech",
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
# HuggingFace 数据流（JSON 接口，不是 RSS，走 fetcher 里的专用抓取函数）
# --------------------------------------------------------------------------
# 这两个接口是"每日热门模型/论文"的权威来源，实测均返回 200：
#   daily_papers    —— 社区投票的每日热门论文，带 upvotes / 摘要
#   trending models —— 按 trendingScore 排序的热门模型，带 likes / downloads
# 它们不是 feed，所以不能塞进 RSS_SOURCES（feedparser 解析 JSON 会直接失败）。
HF_ENABLED = os.getenv("HF_ENABLED", "true").lower() not in ("0", "false", "no")
HF_DAILY_PAPERS_URL = "https://huggingface.co/api/daily_papers"
HF_TRENDING_MODELS_URL = (
    "https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=25"
)
HF_PAPERS_LIMIT = int(os.getenv("HF_PAPERS_LIMIT", "25"))
HF_MODELS_LIMIT = int(os.getenv("HF_MODELS_LIMIT", "15"))

# HF daily_papers 是滚动榜单，不是"过去 24 小时"的流：实测最新一篇也常是两三
# 天前提交的。用全局 24 小时窗口会把这个源整个静默清空，所以单独给它一个更宽的窗口。
HF_LOOKBACK_HOURS = int(os.getenv("HF_LOOKBACK_HOURS", "48"))


# --------------------------------------------------------------------------
# 内容筛选引擎（"主编级"前置过滤）
# --------------------------------------------------------------------------
# 设计取向：**先用零成本的本地规则把池子收窄，再让 DeepSeek 在收窄后的池子上做价值判定。**
#
# 为什么不直接让模型对几百条原始新闻打分：那样每次要把几百条标题+摘要塞进上下文，
# 输入 token 会从现在的 ~13k 涨到 40k 以上，而且模型对"哪条更重要"的判断在长列表上
# 反而更不稳定（中间位置的条目容易被忽略，即位置偏差）。本地规则先砍掉明显跑题的部分，
# 既省钱又让模型的注意力集中在真正需要判断的条目上。
#
# 三道闸门，顺序固定：
#   ① 硬否决（CONTENT_VETO_KEYWORDS）—— 与产业动态无关的类别，直接丢弃
#   ② 本地加权评分（SECTION_HINTS / POSITIVE / NEGATIVE）—— 排序，取前 N 进重排
#   ③ 强触发白名单（GEEK_FORCE_KEYWORDS）—— 命中则无条件入选，并豁免①②

# ① 硬否决：命中即丢弃。
# 依据是最近一期早报里 38% 的条目明显跑题，且都是这几类：选举政治、军事与情报、
# 宏观货币政策、股市行情、司法诉讼、人事任免。用户明确要求丢弃人事变动与诉讼。
CONTENT_VETO_KEYWORDS = [
    # 政治 / 选举
    "选举", "议会", "参选", "选民", "竞选", "民调", "白宫", "国会",
    # 军事 / 情报
    "国防部", "军方", "军事", "军演", "袭击", "停火", "条约", "情报机构", "UAP", "不明异常现象",
    # 宏观货币 / 财政（不是产业动态）
    "美联储", "加息", "降息", "央行", "国债", "通胀数据", "非农",
    # 股市 / 资本市场行情（个股涨跌属于行情播报，不是产业新闻）
    "股指", "A股", "港股", "美股", "涨停", "跌停", "大盘", "板块逆势", "粮食板块",
    "基金净值", "回购计划", "增持", "减持",
    # 司法 / 纠纷
    "起诉", "被诉", "诉讼", "被判", "判决", "罚款", "仲裁", "反垄断调查",
    # 人事任免
    "任命", "离职", "跳槽", "接任", "卸任", "辞任", "人事变动",
    # 英文否决词。**这一组同样是必需的**：国际源全是英文，只写中文否决词的话
    # "OpenAI sued by ..." / "... antitrust fine" 这类会大摇大摆走进早报。
    "lawsuit", "lawsuits", "sues", "sued", "antitrust", "indicted", "convicted",
    "plea deal", "settlement", "election", "senator", "congressman", "pentagon",
    "military", "ceasefire", "layoffs", "lays off", "resigns", "steps down",
    "quarterly earnings", "stock price", "shares fall", "shares rise", "ipo",
]

# 强触发白名单：命中即**无条件入选**，且豁免上面的硬否决。
#
# ⚠️ 词表刻意收窄。用户原始方案里举的例子包含 `LLM`，但实测 `AI` 一词就能命中
# 最终稿 34% 的条目 —— 用这类宽泛类别词做"强制入选"，等于没有筛选，会把池子里
# 所有带 AI 字样的东西全放进来。所以这里只放**具名实体**（模型/产品/公司/技术专名）
# 和少数没有歧义的前沿术语，不放 AI / LLM / 模型 / 芯片 这类泛化词
#（它们改走下面的 POSITIVE 加权，用来排序而不是用来强制）。
GEEK_FORCE_KEYWORDS = [
    # 具名模型 / 产品
    "jev", "gpt-5", "gpt-6", "gpt5", "gpt6", "claude", "gemini", "llama", "qwen",
    "deepseek", "mistral", "grok", "sora", "kimi", "glm", "minimax", "step-audio",
    "openclaw", "dify", "n8n", "langchain", "comfyui", "transformers", "vllm", "ollama",
    # 前沿技术专名（歧义低）
    "agi", "sota", "state-of-the-art", "open-weight", "开源权重", "开源模型",
    "moe", "mixture-of-experts", "混合专家", "推理模型", "reasoning model",
    "强化学习", "rlhf", "rlvr", "思维链", "chain-of-thought", "蒸馏",
    "世界模型", "world model", "扩散模型", "diffusion", "多模态", "multimodal",
    "ai agent", "智能体", "具身智能", "embodied", "算力集群", "超节点",
    "量子计算", "quantum computing", "神经接口", "bci",
]

# 强触发词是否豁免硬否决。
# 取 True 的理由：漏掉一条硬核新闻（用户正在抱怨的问题）的代价，远高于多收一条
# 带点噪音但确实和 AI 沾边的新闻。所以宁可放宽。想收紧就改成 False。
GEEK_FORCE_BYPASSES_VETO = True

# ② 本地加权：命中即加分。不给硬否决权，只影响排序。
POSITIVE_KEYWORDS = {
    "发布": 3, "开源": 4, "上线": 2, "推出": 2, "突破": 3, "刷新": 2, "登顶": 3,
    "benchmark": 3, "评测结果": 2, "榜首": 2, "sota": 4, "刷新纪录": 3,
    "模型": 2, "大模型": 3, "算法": 2, "训练": 2, "推理": 2, "微调": 2,
    "芯片": 2, "gpu": 2, "算力": 3, "数据中心": 2, "集群": 2,
    "协议": 2, "框架": 2, "sdk": 2, "api": 1, "命令行": 1, "自托管": 3,
    "安全漏洞": 2, "cve": 2, "后门": 2, "逆向": 1,
    "agent": 3, "智能体": 3, "rag": 2, "mcp": 3, "上下文": 1,
}

# 弱相关：命中即减分（数码评测/消费导购类，用户明确要求丢弃）
NEGATIVE_KEYWORDS = {
    "评测": 3, "上手": 3, "开箱": 4, "图赏": 4, "实拍": 3, "晒单": 4,
    "体验报告": 3, "值得买": 4, "优惠": 3, "折扣": 3, "促销": 3, "发布会回顾": 2,
    "传闻": 2, "爆料": 2, "疑似": 1, "网传": 3,
}

# 板块相关性画像：进入某个板块的条目至少命中一个该板块的"正词"，
# 否则被相关性闸门拦下（除非命中强触发词）。
# 这一条专治"中新网财经的宏观新闻被标成 consumer 塞进国内新消费"那类问题。
SECTION_HINTS = {
    "intl_tech": [
        # 产业与硬件
        "ai", "模型", "大模型", "语言模型", "芯片", "半导体", "算力", "云", "数据中心",
        "机器人", "自动驾驶", "航天", "量子", "操作系统", "浏览器", "安全", "漏洞",
        # 开发者与软件工程（这一组是必需的：缺了它，一条"某框架发布新版本"的
        # 开发者工具新闻会命中不了任何话题词，被相关性闸门静默误杀 —— 正是要避免的漏报）
        "开源", "算法", "开发者", "开发", "编程", "代码", "工程", "架构", "部署",
        "自托管", "框架", "sdk", "api", "协议", "插件", "扩展", "编辑器", "终端",
        "命令行", "cli", "容器", "docker", "kubernetes", "数据库", "前端", "后端",
        "agent", "智能体", "自动化", "效率", "工具", "平台", "软件", "硬件",
        "模型训练", "推理", "微调", "蒸馏", "多模态", "数据集",
        "startup", "融资", "发布", "版本",
    ],
    "cn_tech": [
        "ai", "模型", "大模型", "语言模型", "芯片", "半导体", "算力", "云", "数据中心",
        "机器人", "自动驾驶", "航天", "量子", "操作系统", "鸿蒙", "安全", "漏洞",
        "开源", "算法", "开发者", "开发", "编程", "代码", "工程", "架构", "部署",
        "自托管", "框架", "sdk", "api", "协议", "插件", "扩展", "编辑器", "终端",
        "命令行", "cli", "容器", "docker", "数据库", "前端", "后端", "国产", "厂商",
        "agent", "智能体", "自动化", "效率", "工具", "平台", "软件", "硬件",
        "模型训练", "推理", "微调", "蒸馏", "多模态", "数据集", "新能源",
        "发布", "版本",
    ],
    "intl_consumer": [
        # 中文词（国内源也可能落进这个板块）
        "零售", "消费", "品牌", "门店", "电商", "超市", "餐饮", "服饰", "美妆",
        "食品", "饮料", "购物", "营销", "渠道", "供应链", "出海", "会员", "订阅",
        "定价", "消费者", "涨价", "降价",
        # 英文词 —— 这一组是必需的：这个板块的源（Retail Dive / Modern Retail）
        # 全是英文站，只写中文话题词会让整个板块**永远匹配不到任何条目**，
        # 页面天天显示"本时段无内容"。实测踩过这个坑。
        "retail", "consumer", "brand", "store", "stores", "ecommerce", "e-commerce",
        "grocery", "restaurant", "apparel", "fashion", "beauty", "food", "beverage",
        "shopper", "shoppers", "shopping", "marketing", "supply chain", "subscription",
        "pricing", "discount", "dtc", "cpg", "merchandise", "outlet", "franchise",
        "marketplace", "cosmetics", "footwear", "luxury", "loyalty",
    ],
    "cn_consumer": [
        "零售", "消费", "品牌", "门店", "电商", "超市", "餐饮", "服饰", "美妆",
        "食品", "饮料", "购物", "营销", "渠道", "供应链", "出海", "会员", "订阅",
        "定价", "消费者", "涨价", "降价", "直播间", "即时零售", "团购", "外卖",
        "会员店", "折扣店", "便利店", "母婴", "宠物", "家居", "家电",
    ],
}

# ③ 重排与配额
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "true").lower() not in ("0", "false", "no")
# 送进 DeepSeek 重排的候选上限（每个板块）。压住这个数就是压住 token 成本。
RERANK_POOL_PER_SECTION = int(os.getenv("RERANK_POOL_PER_SECTION", "25"))
# 重排调用的字符预算。超出后压摘要长度而不是丢条目（见 summarizer._fit_rerank_budget）。
RERANK_MAX_INPUT_CHARS = int(os.getenv("RERANK_MAX_INPUT_CHARS", "24000"))
# 重排后每个板块保留多少条 —— 4 个新闻板块 × 5 = 20 条，落在用户要求的 15-20 区间
RERANK_KEEP_PER_SECTION = int(os.getenv("RERANK_KEEP_PER_SECTION", "5"))
# 每个板块为强触发词预留的保底名额（命中强触发词的条目一定进得来）
FORCE_RESERVED_PER_SECTION = int(os.getenv("FORCE_RESERVED_PER_SECTION", "2"))

# --------------------------------------------------------------------------
# GitHub 热门项目采集
# --------------------------------------------------------------------------
# GitHub 热门项目的筛选标签。
# ⚠️ 注意：不是所有想当然的 topic 名都真的存在。
# 实测 `tools-for-developers` 在 GitHub 上**零仓库使用**（查询稳定返回 0 条），
# 它此前一直在静默缩小候选池。已换成确实有内容的 `developer-tools`。
# 新增 topic 后建议先验证：topic:<名字> stars:>=8000 能不能查出东西。
GITHUB_TOPICS = [
    "ai",
    "machine-learning",
    "productivity",
    "developer-tools",
]

# 「老将新动作」用的主题。这个主题本身要有足够体量，否则叠加其他条件后容易查空。
GITHUB_REVIVED_TOPIC = os.getenv("GITHUB_REVIVED_TOPIC", "productivity")

GITHUB_MIN_STARS = int(os.getenv("GITHUB_MIN_STARS", "50"))
GITHUB_PER_TOPIC_LIMIT = int(os.getenv("GITHUB_PER_TOPIC_LIMIT", "15"))
GITHUB_TOTAL_LIMIT = int(os.getenv("GITHUB_TOTAL_LIMIT", "30"))

# --------------------------------------------------------------------------
# GitHub 板块：3 + 2 混合推荐
#
#   3 个「近期趋势」  —— 过去 N 天内新建或重新活跃的项目，保证每天有新鲜血液
#   2 个「历史经典」  —— 按总星数排名分页轮播，用游标状态文件推进
#
# 条数固定为 5，不再让模型从几十个候选里挑。
# --------------------------------------------------------------------------
GITHUB_TREND_COUNT = int(os.getenv("GITHUB_TREND_COUNT", "3"))
GITHUB_CLASSIC_COUNT = int(os.getenv("GITHUB_CLASSIC_COUNT", "2"))
GITHUB_DISPLAY_COUNT = GITHUB_TREND_COUNT + GITHUB_CLASSIC_COUNT

# 「趋势」的时间窗。注意 GitHub Search API 没有"星标增长速度"这个指标，
# 只能用「创建时间 + 星数排序」来近似"近期冒头"。
GITHUB_TREND_DAYS = int(os.getenv("GITHUB_TREND_DAYS", "7"))
GITHUB_TREND_STAR_FLOOR = int(os.getenv("GITHUB_TREND_STAR_FLOOR", "30"))
# 「月度新秀」：创建 7–30 天，已经是这个月冒出来的，仍算新鲜
GITHUB_TREND_WIDER_DAYS = int(os.getenv("GITHUB_TREND_WIDER_DAYS", "30"))
GITHUB_TREND_WIDER_STAR_FLOOR = int(os.getenv("GITHUB_TREND_WIDER_STAR_FLOOR", "800"))

# 「近期翻红」的星数上限。
# 实测教训：不加这个上限时，pushed:>=7d 会稳定返回 freeCodeCamp(455k)、public-apis(480k)
# 这类每天都在推送的巨型仓库 —— 它们不是"趋势"，而是永久霸榜的常驻民。
# 加上限后，这个查询才真正对应"中等规模、最近突然活跃起来"的项目。
GITHUB_REVIVED_STAR_FLOOR = int(os.getenv("GITHUB_REVIVED_STAR_FLOOR", "2000"))
GITHUB_REVIVED_STAR_CEILING = int(os.getenv("GITHUB_REVIVED_STAR_CEILING", "40000"))
GITHUB_REVIVED_MIN_AGE_DAYS = int(os.getenv("GITHUB_REVIVED_MIN_AGE_DAYS", "30"))

# 「经典」池：按星数排序后，每个 topic 各取前 N 个再合并排序
# 池子越大，轮播周期越长。4 个 topic × 40 ≈ 去重后 100 个左右，够轮 50 天。
GITHUB_CLASSIC_STAR_FLOOR = int(os.getenv("GITHUB_CLASSIC_STAR_FLOOR", "8000"))
GITHUB_CLASSIC_POOL_PER_TOPIC = int(os.getenv("GITHUB_CLASSIC_POOL_PER_TOPIC", "40"))
# 池子最长复用天数。到期后重建，让新晋高星项目有机会进入轮播。
GITHUB_CLASSIC_POOL_MAX_AGE_DAYS = int(os.getenv("GITHUB_CLASSIC_POOL_MAX_AGE_DAYS", "14"))

# 记住最近推荐过多少个「趋势」项目，下次优先避开，保证每日不重样
GITHUB_RECENT_MEMORY = int(os.getenv("GITHUB_RECENT_MEMORY", "40"))

# 要求仓库至少打了一个 topic。
# 依据：实测混进来过一个叫 ai-sucks-butt 的抗议型仓库（"觉得 AI 不行就点个星"），
# 它有 Python 语言标记所以按语言过滤拦不住，但它是**零 topic**的；
# 而同期 6 个真工具（threeui / next-ai-draw-io / ECC / hermes-agent / mural / m3e-canvas）
# 全都有 topic。认真做的项目基本都会给自己打标签，零 topic 常是低投入或玩票项目。
# 觉得误伤太多可以设成 false 关掉。
GITHUB_REQUIRE_TOPICS = os.getenv("GITHUB_REQUIRE_TOPICS", "true").lower() not in (
    "0", "false", "no",
)

# 轮播游标状态文件。放在 archive/ 下，会被 Actions 的存档回写步骤一起提交进仓库，
# 于是状态自然持久化，不依赖任何外部存储。
GITHUB_STATE_FILE = ARCHIVE_DIR / "github_offset.json"

# README 深度解析：抓取每个仓库 README 的字符上限，以及最多抓多少个仓库
# 注意 /readme 走的是 core 配额（5000/小时），与 Search 的 30 次/分钟是两套独立配额
README_CHAR_LIMIT = int(os.getenv("README_CHAR_LIMIT", "1500"))
README_MAX_REPOS = int(os.getenv("README_MAX_REPOS", "30"))

# GitHub 板块是独立的一次 DeepSeek 调用，上下文预算单独给。
# README 摘录很占空间（1500 字符/个），预算给足才能让更多仓库带着上下文进模型。
# 45000 字符约合 15k tokens，deepseek-chat 的上下文完全放得下。
GITHUB_MAX_INPUT_CHARS = int(os.getenv("GITHUB_MAX_INPUT_CHARS", "45000"))

# 明显偏离"效率与 AI 工具"主题的仓库会被关键词过滤掉。
# 这里主要拦两类：① 精选清单/导航类（awesome-*）② 教程/学习资料/面试题库。
# 它们星数极高、topics 也常打 ai/productivity，如果不拦会霸占整个板块。
GITHUB_EXCLUDE_KEYWORDS = [
    "awesome-list-of-awesome-lists",
    "interview-questions",
    "leetcode-solutions",
    "dotfiles",
    "wallpaper",
    "cheatsheet-collection",
    "free-programming-books",
    "freecodecamp",
    "public-apis",
    "system-design-primer",
    "developer-roadmap",
    "coding-interview",
    "build-your-own-x",
    "project-based-learning",
    "the-book-of-secret-knowledge",
    "programming-books",
    "computer-science",
    "learning-resources",
    "curated-list",
    "collection-of",
]

# 仓库名本身的排除规则（正则，小写匹配）。
# 名字里带这些词的几乎必然是清单或教程，比 topics/description 更可靠。
GITHUB_EXCLUDE_NAME_PATTERNS = [
    r"^awesome-",
    r"-awesome$",
    r"^awesome$",
    r"^free-",
    r"-books?$",
    r"^books?-",
    r"roadmap",
    r"cheat-?sheet",
    r"^tutorial",
    r"-tutorial",
    r"^learn-",
    r"-guide$",
    r"-handbook$",
    r"^papers-",
    r"-papers$",
    r"^interview",
    r"interview-",
    r"^100-",
    r"^50-",
    r"^30-",
    # 教程 / 课程 / 学习资料类：星数虚高，但属于"读物"而不是"工具"
    r"for-beginners",
    r"from-scratch",
    r"made-with-",
    r"-examples?$",
    r"-course",
    r"-notes$",
    r"video-courses",
    r"^d2l",
    r"-curriculum",
]

# --------------------------------------------------------------------------
# Obsidian 导出
# --------------------------------------------------------------------------
# 每期早报除了发布网页，还会在 obsidian/ 下额外导出一份带 YAML 前置区与双链的
# Markdown（obsidian/YYYY-MM-DD.md），方便直接丢进 Obsidian 库。
#
# 固定标签：每份 Markdown 的 tags 里都会带上这几个，再拼上模型提取的关键词。
# 改成 "早报,AI,科技" 这样用逗号分隔即可。
OBSIDIAN_FIXED_TAGS = [
    t.strip() for t in os.getenv("OBSIDIAN_FIXED_TAGS", "早报,AI").split(",") if t.strip()
]

# 模型提取的关键词最多取几个（固定标签不计入这个上限）
OBSIDIAN_MAX_KEYWORDS = int(os.getenv("OBSIDIAN_MAX_KEYWORDS", "5"))


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
