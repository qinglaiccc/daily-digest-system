#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
离线自检脚本。

不需要任何 API Key，也不需要联网，用于验证各模块的纯逻辑部分：
    python selftest.py

覆盖范围：
1. 文本清洗、URL 归一化
2. 防幻觉链接校验（UrlGuard）—— 模型编造的链接必须被丢弃
3. 模型 JSON 的宽松解析（含 ```json 围栏、前后废话）
4. 新闻/GitHub 两条归一化链路的字段收敛与条数限制
5. 降级方案 build_fallback_result
6. 邮件推送（SMTP）的主题、纯文本与 HTML 组装
7. 页面渲染完整性（无 Jinja 残留、板块齐全、链接可用）
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

import config
import fetcher
import notifier
import renderer
import summarizer

PASSED = 0
FAILED = 0

# 自检用的采集统计（模拟一次完整运行的采集结果）
STATS = {
    "rss_sources": 24,
    "rss_ok": 24,
    "rss_failed": 0,
    "raw_news": 190,
    "github_queries": 8,
}


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


# --------------------------------------------------------------------------
def make_news() -> list[fetcher.NewsItem]:
    now = datetime.now(timezone.utc)
    return [
        fetcher.NewsItem(
            title="OpenAI 发布新一代推理模型",
            url="https://techcrunch.com/2026/09/13/openai-new-model/",
            source="TechCrunch",
            summary="OpenAI 今日发布新模型，推理能力提升。",
            published=now - timedelta(hours=2),
            region="intl",
            category="tech",
        ),
        fetcher.NewsItem(
            title="某国内厂商发布新款芯片",
            url="https://www.ithome.com/0/800/123.htm",
            source="IT之家",
            summary="该芯片采用 3nm 工艺。",
            published=now - timedelta(hours=5),
            region="cn",
            category="tech",
        ),
        fetcher.NewsItem(
            title="某咖啡品牌门店数破万",
            url="https://www.digitaling.com/articles/1234567.html",
            source="刀法研究所",
            summary="门店数量突破一万家。",
            published=now - timedelta(hours=8),
            region="cn",
            category="consumer",
        ),
    ]


def make_repos() -> list[fetcher.RepoItem]:
    return [
        fetcher.RepoItem(
            full_name="example/cool-agent",
            url="https://github.com/example/cool-agent",
            description="一个 AI Agent 框架",
            stars=1234,
            language="Python",
            topics=["ai", "agents"],
            created_at=datetime.now(timezone.utc) - timedelta(hours=6),
            pushed_at=datetime.now(timezone.utc),
            is_new=True,
        )
    ]


# --------------------------------------------------------------------------
def test_text_utils() -> None:
    print("\n[1] 文本清洗与 URL 归一化")

    cleaned = fetcher.clean_text("<p>Hello&nbsp;<b>World</b></p>\n\n\n\n第二段", limit=100)
    check("HTML 标签被剥离", "<" not in cleaned and ">" not in cleaned, cleaned)
    check("HTML 实体被解码", "&nbsp;" not in cleaned, cleaned)

    truncated = fetcher.clean_text("啊" * 500, limit=50)
    check("超长文本被截断", len(truncated) <= 50, f"len={len(truncated)}")

    cases = [
        ("https://www.techcrunch.com/2026/09/13/foo/", "techcrunch.com/2026/09/13/foo"),
        ("https://github.com/owner/repo/tree/main/src", "github.com/owner/repo"),
        ("http://Example.COM/Path?utm_source=x#frag", "example.com/Path"),
    ]
    for raw, expected in cases:
        got = summarizer.normalize_url(raw)
        check(f"归一化 {raw[:42]}", got == expected, f"期望 {expected}，实际 {got}")


def test_url_guard() -> None:
    print("\n[2] 防幻觉链接校验")

    guard = summarizer.UrlGuard(make_news(), make_repos())

    ok = guard.resolve("https://techcrunch.com/2026/09/13/openai-new-model/", "任意标题")
    check("真实链接可通过", ok is not None and ok["url"].startswith("https://techcrunch.com"))

    ok = guard.resolve("https://github.com/example/cool-agent", "example/cool-agent")
    check("GitHub 仓库链接可通过", ok is not None)

    ok = guard.resolve("https://github.com/example/cool-agent/tree/main/docs", "example/cool-agent")
    check(
        "带路径的 GitHub 链接归一到仓库主页",
        ok is not None and ok["url"].endswith("cool-agent"),
        str(ok),
    )

    fake = guard.resolve("https://totally-made-up-news.com/2026/fake", "完全不存在的新闻标题")
    check("编造链接被拒绝", fake is None)

    by_title = guard.resolve("", "OpenAI 发布新一代推理模型")
    check("无链接时按标题回查成功", by_title is not None and "techcrunch" in by_title["url"])

    repo_by_name = guard.resolve("", "example/cool-agent")
    check(
        "仓库仅给出 owner/repo 名称时可对回",
        repo_by_name is not None and repo_by_name["url"] == "https://github.com/example/cool-agent",
        str(repo_by_name),
    )

    fake_title = guard.resolve("", "这条新闻根本不存在于素材中")
    check("标题也编造时被拒绝", fake_title is None)


def test_json_parsing() -> None:
    print("\n[3] 模型返回的宽松 JSON 解析")

    plain = summarizer._parse_json_response('{"date": "2026-09-13"}')
    check("纯 JSON 可解析", plain.get("date") == "2026-09-13")

    fenced = summarizer._parse_json_response('```json\n{"a": 1}\n```')
    check("```json 围栏可解析", fenced.get("a") == 1)

    chatty = summarizer._parse_json_response('好的，以下是结果：\n{"a": 2}\n希望有帮助！')
    check("前后夹带文字可解析", chatty.get("a") == 2)

    try:
        summarizer._parse_json_response("这里完全没有 JSON")
        check("无 JSON 时抛错", False, "未抛异常")
    except ValueError:
        check("无 JSON 时抛错", True)


def test_normalize_result() -> None:
    print("\n[4] 模型输出归一化（含幻觉过滤与条数限制）")

    guard = summarizer.UrlGuard(make_news(), make_repos())
    today = summarizer.today_local()

    # 构造一个含幻觉链接、超量条目、缺字段的"坏"模型输出
    over_limit = [
        {
            "title": f"真实新闻 {i}",
            "summary": "总结",
            "source": "TechCrunch",
            "url": "https://techcrunch.com/2026/09/13/openai-new-model/",
        }
        for i in range(config.ITEMS_PER_SECTION + 5)
    ]
    raw = {
        "date": today.isoformat(),
        "digest": "今日导读",
        "sections": {
            "intl_tech": over_limit
            + [
                {
                    "title": "编造新闻",
                    "summary": "总结",
                    "source": "假媒体",
                    "url": "https://fake-news-site.example/hallucinated",
                }
            ],
            "github": [
                {
                    "title": "example/cool-agent",
                    "summary": "AI Agent 框架",
                    "source": "GitHub",
                    "url": "https://github.com/example/cool-agent",
                    "stars": "1234",
                    "language": "Python",
                }
            ],
            "cn_tech": "这不是一个列表",  # 类型错误，应被容错
        },
    }

    sections, digest, dropped = summarizer.normalize_news_result(raw, guard, today)
    by_key = {s["key"]: s["items"] for s in sections}

    check("新闻部分为四个板块", len(sections) == 4, str(len(sections)))
    check(
        f"条数被限制在 {config.ITEMS_PER_SECTION} 条内",
        len(by_key["intl_tech"]) == config.ITEMS_PER_SECTION,
        str(len(by_key["intl_tech"])),
    )
    check(
        "幻觉链接被剔除",
        all("fake-news-site" not in i["url"] for i in by_key["intl_tech"]),
    )
    check("被剔除的条目计入 dropped", dropped == 1, str(dropped))
    check("类型错误的板块被容错为空列表", by_key["cn_tech"] == [])
    check("导读被保留", digest == "今日导读")
    check("github 不再由新闻提示词产出", "github" not in by_key)


def test_github_structure() -> None:
    """GitHub 板块的结构化解析：四件套字段、星数取真实值、命令不得编造。"""
    print("\n[5] GitHub 板块深度解析结构")

    repos = make_repos()
    guard = summarizer.UrlGuard([], repos)

    raw = {
        "digest_repos": "这批项目都在给 AI 助手做记忆和工具调用",
        "repos": [
            {
                "repo": "example/cool-agent",
                "url": "https://github.com/example/cool-agent",
                "intro": "一句话就能搭起一个会自己干活的 AI 助手。",
                "features": ["自动拆解任务", "支持多种大模型", "本地运行不上传数据"],
                "guide": "需要先装 Node 18，然后一条命令就能跑起来。",
                "install": "npx cool-agent init",
                # 故意填一个错误的星数，验证程序会以采集到的真实值覆盖它
                "stars": "999999",
                "language": "Rust",
            },
            # 伪造的仓库，必须被丢弃
            {
                "repo": "evil/fake-repo",
                "url": "https://github.com/evil/fake-repo",
                "intro": "这是编造的项目",
                "features": ["假的"],
            },
        ],
    }

    items, lead, dropped = summarizer.normalize_github_result(raw, repos, guard)

    check("只保留能验证的仓库", len(items) == 1, f"{len(items)} 条")
    check("编造仓库被丢弃", dropped == 1, str(dropped))
    check("板块导语被保留", lead.startswith("这批项目"), lead)

    item = items[0]
    check("含项目通俗简介 intro", bool(item.get("intro")), item.get("intro", ""))
    check("含核心功能亮点 features", len(item["features"]) == 3, str(len(item["features"])))
    check("含应用与部署指南 guide", bool(item.get("guide")), item.get("guide", ""))
    check("含一键命令 install", item.get("install") == "npx cool-agent init")
    check("星数取采集真实值而非模型值", item["stars"] == "1234", item["stars"])
    check("语言取采集真实值而非模型值", item["language"] == "Python", item["language"])
    check("summary 与 intro 对齐（兼容邮件通道）", item["summary"] == item["intro"])
    check("仓库名保留英文原名", item["title"] == "example/cool-agent")

    # 亮点字段的容错：字符串、带项目符号、超量
    messy = {
        "repos": [
            {
                "repo": "example/cool-agent",
                "url": "https://github.com/example/cool-agent",
                "intro": "简介",
                "features": "- 带符号的亮点\n- 第二条\n- 第三条\n- 第四条\n- 第五条\n- 第六条",
                "guide": "",
                "install": "",
            }
        ]
    }
    items2, _, _ = summarizer.normalize_github_result(messy, repos, guard)
    feats = items2[0]["features"]
    check("字符串形式的亮点被拆分", len(feats) >= 3, str(feats))
    check("亮点前缀符号被清理", all(not f.startswith("-") for f in feats), str(feats))
    check("亮点数量被限制在 5 条内", len(feats) <= 5, str(len(feats)))

    # 完全没有结构的仓库应退回原始 description，而不是留空
    bare = {
        "repos": [
            {
                "repo": "example/cool-agent",
                "url": "https://github.com/example/cool-agent",
                "intro": "",
                "features": [],
            }
        ]
    }
    items3, _, dropped3 = summarizer.normalize_github_result(bare, repos, guard)
    check("无简介无亮点时整条丢弃", len(items3) == 0 and dropped3 == 1)


def test_fallback() -> None:
    print("\n[6] 降级方案")

    result = summarizer.build_fallback_result(
        make_news(), make_repos(), summarizer.today_local(), "测试原因"
    )
    by_key = {s["key"]: s["items"] for s in result["sections"]}

    check("标记为降级", result.get("degraded") is True)
    check("五个板块齐全", len(result["sections"]) == 5)
    check("国内科技板块有内容", len(by_key["cn_tech"]) == 1)
    check("国内消费板块有内容", len(by_key["cn_consumer"]) == 1)
    check("GitHub 板块有内容", by_key["github"][0]["title"] == "example/cool-agent")
    check("每条都有链接", all(i["url"] for i in by_key["cn_tech"] + by_key["github"]))
    check("降级的 GitHub 条目含结构化字段", "intro" in by_key["github"][0])

    # 部分降级：只降新闻，GitHub 结果保留
    news_sections, n = summarizer.build_fallback_news_section(make_news(), "测试")
    gh_items = summarizer.build_fallback_github_section(make_repos())
    check("可单独降级新闻部分", n == 4 and len(news_sections) == 4)
    check("可单独降级 GitHub 部分", len(gh_items) == 1 and gh_items[0]["intro"])


def test_notifier() -> None:
    print("\n[7] 邮件推送（SMTP）")

    report = summarizer.build_fallback_result(
        make_news(), make_repos(), summarizer.today_local(), "自检"
    )
    report["_raw_news"] = 190
    site = "https://example.github.io/daily-digest-system/"

    subject = notifier.build_subject(report)
    check("主题含日期", "每日早报" in subject and "09月" in subject, subject)
    check("主题长度受限", len(subject) <= config.EMAIL_SUBJECT_MAX, f"{len(subject)} 字符")
    check("降级时主题带标记", notifier.build_subject(
        {**report, "degraded": True, "degraded_reason": "x"}
    ).startswith("[降级]"))

    plain = notifier.build_plain_text(report, site)
    check("纯文本含网页链接", site in plain)
    check("纯文本含各板块", all(s["title"] in plain for s in report["sections"]))
    check("纯文本含原文链接", "原文：https://" in plain)

    html = notifier.build_html(report, site)
    check("HTML 含 doctype", html.lstrip().startswith("<!DOCTYPE html>"))
    check("HTML 用表格布局（邮件客户端兼容）", '<table role="presentation"' in html)
    check("HTML 全部为行内样式", "style=" in html and "<style" not in html.lower())
    check("HTML 无现代布局属性", "display:flex" not in html and "display:grid" not in html)
    check("HTML 含网页版入口", site in html)
    check("HTML 含阅读全文链接", "阅读全文" in html)
    check("HTML 使用瑞士红点缀", config_hex_accent(html))
    check("危险字符被转义", "<script" not in html.lower())

    # 转义：标题里带尖括号不应破坏结构
    tricky = summarizer.build_fallback_result(
        [
            fetcher.NewsItem(
                title='<script>alert("xss")</script> & "引号"',
                url="https://example.com/a",
                source="测试",
                summary="含 <b>标签</b> 与 & 符号",
                published=datetime.now(timezone.utc),
                region="cn",
                category="tech",
            )
        ],
        [],
        summarizer.today_local(),
        "自检",
    )
    tricky_html = notifier.build_html(tricky, site)
    check("标题中的脚本标签被转义", "<script>alert" not in tricky_html)
    check("裸 & 被转义", " & " not in tricky_html.replace("&amp;", "").replace("&nbsp;", "").replace("&rsaquo;", ""))

    # 未配置 SMTP 时应安全跳过而不是抛异常
    saved = (config.SMTP_USER, config.SMTP_PASS, config.RECEIVER_EMAIL)
    try:
        config.SMTP_USER = ""
        config.SMTP_PASS = ""
        config.RECEIVER_EMAIL = ""
        check("未配置 SMTP 时安全跳过", notifier.send_email(report) is False)
        check("dry-run 时直接返回成功", notifier.send_email(report, dry_run=True) is True)
    finally:
        config.SMTP_USER, config.SMTP_PASS, config.RECEIVER_EMAIL = saved

    check("收件人去重且保序", config.resolve_recipients() == list(dict.fromkeys(config.resolve_recipients())))


def config_hex_accent(html: str) -> bool:
    return "#e4002b" in html.lower()


def test_render() -> None:
    print("\n[8] 页面渲染（Dashboard 复古纸质风格）")

    import shutil
    import tempfile

    # 关键：绝不能写进真实的 dist/，否则会把已经生成好的正式产物覆盖成测试假数据
    original = (config.DIST_DIR, config.OUTPUT_HTML)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="digest-render-"))

    try:
        config.DIST_DIR = tmp
        config.OUTPUT_HTML = tmp / "index.html"

        report = summarizer.build_fallback_result(
            make_news(), make_repos(), summarizer.today_local(), "自检"
        )
        # 注入一条带完整结构化字段的 GitHub 条目，验证四件套能真正落到页面上
        for sec in report["sections"]:
            if sec["key"] == "github":
                sec["lead"] = "这批项目都在让 AI 助手更好用"
                sec["items"] = [
                    {
                        "title": "example/cool-agent",
                        "url": "https://github.com/example/cool-agent",
                        "intro": "一句话就能搭起一个会自己干活的 AI 助手。",
                        "features": ["自动拆解任务", "支持多种大模型", "本地运行不上传数据"],
                        "guide": "需要先装 Node 18，然后一条命令就能跑起来。",
                        "install": "npx cool-agent init",
                        "summary": "一句话就能搭起一个会自己干活的 AI 助手。",
                        "source": "GitHub",
                        "stars": "1234",
                        "language": "Python",
                        "is_new": True,
                        "title_original": "",
                        "excerpt": "An AI agent framework",
                        "is_foreign": True,
                    }
                ]

        out = renderer.render_issue(report, STATS, [], config.OUTPUT_HTML, root="")
        html = out.read_text(encoding="utf-8")

        check("文件已生成", out.exists() and out.stat().st_size > 5000, f"{out.stat().st_size} 字节")
        check("无未渲染的 Jinja 变量", "{{" not in html and "{%" not in html)
        check("声明了 UTF-8", 'charset="UTF-8"' in html)
        check("包含 viewport 元信息（移动端适配）", "viewport" in html)
        check("包含五个板块面板", all(f'id="panel-{k}"' in html for k in config.SECTION_KEYS))
        check("统计口径正确（24 个 RSS 源）", "24 个 RSS 源" in html)

        # 所有外链都必须是 http(s)
        import re

        hrefs = re.findall(r'href="([^"]+)"', html)
        # 内部相对链接（archive/... 、index.html、#anchor）是正常的，只校验真正的"外链"
        externals = [h for h in hrefs if "://" in h or h.startswith("//")]
        check(
            "所有外部链接均为 http(s)",
            all(h.startswith(("http://", "https://")) for h in externals),
            str([h for h in externals if not h.startswith(("http://", "https://"))][:3]),
        )
        check("内部链接均为相对路径", 'href="archive/' in html)
        check("原文链接带 noopener", 'rel="noopener noreferrer nofollow"' in html)

        # ---- 视觉规范合规：复古纸质 + 硬核边框 Dashboard ----
        css = html.split("</style>")[0].lower()

        allowed = {
            "#f4f1ea", "#ebe6da", "#e3ddd0",   # 纸质米色三档底板
            "#111111", "#3d3a34", "#5e5849",   # 纯黑主色 + 两档暖灰
            "#8c2f1f", "#2f5d3a",              # 暗红主点缀 + 深绿次点缀
            "#b9b2a2",                          # 次级分隔线
            "#ffffff",                          # 仅允许出现在颜色的对比/说明性文本里（见下方纯白检查）
        }
        found = {"#" + h for h in re.findall(r"#([0-9a-f]{6})\b", css)}
        stray = found - allowed

        check("色板仅含纸质米色/黑/暖灰/暗红/深绿", not stray, f"越界色值 {sorted(stray)}")
        check("底板为纸质米色 #F4F1EA", "--paper: #f4f1ea" in css)
        check("主点缀色为暗红 #8C2F1F", "#8c2f1f" in css)
        check("次点缀色为深绿 #2F5D3A", "#2f5d3a" in css)

        # 严禁刺眼纯白：背景声明里不能出现 #FFFFFF
        bg_white = re.findall(r"background[^;{}]*#ffffff", css)
        check("背景未使用纯白", not bg_white, str(bg_white[:3]))

        check("零圆角", "border-radius: 0 !important" in css)
        check("零阴影", "box-shadow" not in css)
        check("边框使用纯黑实线", re.search(r"border[^;:]*:\s*[12]px solid var\(--line\)", css) is not None)
        check("全站变量化边框色", "--line: #111111" in css)

        # 布局：双栏 Dashboard
        check("双栏 Grid 布局", "grid-template-columns: var(--sidebar-w) minmax(0, 1fr)" in css)
        check("侧栏固定宽度并吸顶", "--sidebar-w: 268px" in css and "position: sticky" in css)
        check("侧栏当前期反黑高亮", ".archive-nav a.is-current" in css)
        check("KPI 看板区存在", 'class="kpis"' in html and "kpi-value" in css)

        # Tab 交互 + 渐进增强
        check("Tab 使用 tablist 语义", 'role="tablist"' in html and 'role="tab"' in html)
        check("面板使用 tabpanel 语义", 'role="tabpanel"' in html)
        check("默认选中第一个板块", 'aria-selected="true"' in html)
        check("Tab 联动 aria-controls", 'aria-controls="panel-' in html)
        check("无 JS 时全部板块顺序展开",
              ".js-on .panel { display: none; }" in css and "js-on" in html)
        check("支持方向键切换 Tab", "ArrowRight" in html and "ArrowLeft" in html)
        check("支持 #锚点直达板块", "hashchange" in html)

        check("数字使用等宽体", "--mono:" in css and "tabular-nums" in css)
        check("无 emoji 装饰", not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", html))
        check("未用 Unicode 符号当图标", not any(g in html for g in ("▣", "◊", "↗", "★")))
        check("阅读全文为 CSS 绘制标记", 'class="ext"' in html and "阅读全文" in html)

        # GitHub 深度解析结构必须落到页面上
        check("渲染项目通俗简介字段", "项目通俗简介" in html)
        check("渲染核心功能亮点字段", "核心功能亮点" in html)
        check("渲染应用与部署指南字段", "应用与部署指南" in html)
        check("渲染一键安装命令块", 'class="install"' in html or "一键安装" in html)
    finally:
        config.DIST_DIR, config.OUTPUT_HTML = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_language_modes() -> None:
    print("\n[9] 中英对照（语言切换）")

    report = summarizer.build_fallback_result(
        make_news(), make_repos(), summarizer.today_local(), "自检"
    )
    # 写到临时目录，避免在 dist/ 里留下会被发布出去的多余文件
    import tempfile

    with tempfile.TemporaryDirectory(prefix="digest-lang-") as tmp:
        html = renderer.render_issue(
            report, STATS, [], pathlib.Path(tmp) / "lang.html", root=""
        ).read_text(encoding="utf-8")

    check("默认中文模式", 'class="mode-zh"' in html)
    check("提供三种语言模式", all(f'data-lang="{m}"' in html for m in ("zh", "orig", "both")))
    check("中文模式隐藏原文", "body.mode-zh .lang-orig { display: none; }" in html)
    check("原文模式隐藏中文", "body.mode-orig .lang-zh { display: none; }" in html)
    check("语言选择被持久化", "localStorage.setItem" in html and "localStorage.getItem" in html)
    check("切换按钮带无障碍状态", "aria-pressed" in html)

    # 回归断言：原文模式会隐藏所有中文标题，因此每个条目都必须有一份原文标题兜底。
    # 曾经 GitHub 板块因为后端未回填 title_original，在原文模式下整条没有标题。
    item_count = len(re.findall(r'<li class="item\b', html))
    orig_count = len(re.findall(r'<p class="orig-title', html))
    check(
        "每个条目都有原文标题兜底",
        item_count > 0 and orig_count == item_count,
        f"条目 {item_count} 个，原文标题 {orig_count} 个",
    )
    check(
        "双语模式隐藏重复标题",
        "body.mode-both .item:not(.is-foreign) .orig-title" in html
        and "body.mode-both .item.same-title .orig-title" in html,
    )

    # AI 路径：原文标题与摘要应被自动填入
    guard = summarizer.UrlGuard(make_news(), make_repos())
    raw = {
        "date": summarizer.today_local().isoformat(),
        "digest": "导读",
        "sections": {
            "intl_tech": [
                {
                    "title": "OpenAI 发布新推理模型",
                    "summary": "中文摘要",
                    "source": "TechCrunch",
                    "url": "https://techcrunch.com/2026/09/13/openai-new-model/",
                }
            ]
        },
    }
    sections, _, _ = summarizer.normalize_news_result(raw, guard, summarizer.today_local())
    item = next(s for s in sections if s["key"] == "intl_tech")["items"][0]

    check("携带原文标题", item["title_original"] == "OpenAI 发布新一代推理模型", item["title_original"])
    check("携带原文摘要", bool(item["excerpt"]), item["excerpt"][:30])
    check("中文源判定为非外文", item["is_foreign"] is False)

    # 英文素材应被判定为外文
    guard_en = summarizer.UrlGuard(
        [
            fetcher.NewsItem(
                title="OpenAI ships a new reasoning model",
                url="https://techcrunch.com/en/1",
                source="TechCrunch",
                summary="The company said the model improves on math benchmarks.",
                published=datetime.now(timezone.utc),
                region="intl",
                category="tech",
            )
        ],
        [],
    )
    raw_en = {
        "date": summarizer.today_local().isoformat(),
        "digest": "导读",
        "sections": {
            "intl_tech": [
                {
                    "title": "OpenAI 发布新推理模型",
                    "summary": "中文摘要",
                    "source": "TechCrunch",
                    "url": "https://techcrunch.com/en/1",
                }
            ]
        },
    }
    secs_en, _, _ = summarizer.normalize_news_result(raw_en, guard_en, summarizer.today_local())
    item_en = next(s for s in secs_en if s["key"] == "intl_tech")["items"][0]
    check("英文源判定为外文", item_en["is_foreign"] is True)
    check("英文原文标题被保留", item_en["title_original"].startswith("OpenAI ships"))


def test_history() -> None:
    print("\n[10] 往期存档")

    import json as _json
    import shutil
    import tempfile

    original = (config.ARCHIVE_DIR, config.DIST_DIR, config.OUTPUT_HTML, config.HISTORY_MANIFEST)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="digest-history-"))

    try:
        config.ARCHIVE_DIR = tmp / "archive"
        config.DIST_DIR = tmp / "dist"
        config.OUTPUT_HTML = config.DIST_DIR / "index.html"
        config.HISTORY_MANIFEST = config.DIST_DIR / "history.json"

        # 造三天存档
        for offset, digest in ((2, "前天的导读"), (1, "昨天的导读"), (0, "今天的导读")):
            day = (datetime.now(timezone.utc) - timedelta(days=offset)).date()
            rep = summarizer.build_fallback_result(make_news(), make_repos(), day, "自检")
            rep["digest"] = digest
            renderer.save_archive(rep, STATS)

        entries = renderer.load_archive()
        check("读出三份存档", len(entries) == 3, f"{len(entries)} 份")
        check("按日期倒序", [e["date"] for e in entries] == sorted([e["date"] for e in entries], reverse=True))
        check("存档带条数统计", all(e["count"] > 0 for e in entries))

        result = renderer.render_site(entries[0]["report"], STATS)
        check("渲染全部往期页", result["archives_rendered"] == 3, str(result["archives_rendered"]))
        check("今日页存在", (config.DIST_DIR / "index.html").exists())
        check("往期索引页存在", (config.DIST_DIR / "archive" / "index.html").exists())
        check("history.json 存在", config.HISTORY_MANIFEST.exists())

        manifest = _json.loads(config.HISTORY_MANIFEST.read_text(encoding="utf-8"))
        check("manifest 记录 latest", manifest["latest"] == entries[0]["date"])
        check("manifest 列出全部期数", len(manifest["issues"]) == 3)

        arc = (config.DIST_DIR / "archive" / f"{entries[1]['date']}.html").read_text(encoding="utf-8")
        today = (config.DIST_DIR / "index.html").read_text(encoding="utf-8")
        check("往期页用 ../ 相对前缀", 'href="../index.html"' in arc)
        check("往期页有上/下期导航", "更早一期" in arc or "更新一期" in arc)
        check("今日页用无前缀链接", 'href="archive/' in today)
        check("今日页有往期入口", "往期存档" in today)

        # 损坏的存档不应让整站生成失败
        (config.ARCHIVE_DIR / "9999-99-99.json").write_text("{ 这不是 JSON", encoding="utf-8")
        check("损坏存档被跳过", len(renderer.load_archive()) == 3)
    finally:
        (config.ARCHIVE_DIR, config.DIST_DIR, config.OUTPUT_HTML, config.HISTORY_MANIFEST) = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_deepseek_pipeline() -> None:
    """
    用本地 mock 服务模拟 DeepSeek 接口，完整跑一遍 summarize()。

    验证的是真实调用链：OpenAI SDK 构造请求 → HTTP 交互 → JSON 解析 →
    链接校验 → 结果归一化。无需真实 API Key。
    """
    print("\n[11] DeepSeek 调用链（本地 mock 接口）")

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls: list = []

    # 新闻部分的 mock：两条真实链接 + 一条编造链接（应被过滤）
    news_canned = json.dumps(
        {
            "date": summarizer.today_local().isoformat(),
            "digest": "OpenAI 发布新推理模型；国内厂商发布 3nm 芯片。",
            "sections": {
                "intl_tech": [
                    {
                        "title": "OpenAI 发布新一代推理模型",
                        "summary": "OpenAI 发布新模型，推理能力提升。",
                        "source": "TechCrunch",
                        "url": "https://techcrunch.com/2026/09/13/openai-new-model/",
                    },
                    {
                        "title": "编造的新闻",
                        "summary": "这条不该出现。",
                        "source": "假媒体",
                        "url": "https://hallucinated.example.com/fake",
                    },
                ],
                "cn_tech": [
                    {
                        "title": "国内厂商发布新款芯片",
                        "summary": "采用 3nm 工艺。",
                        "source": "IT之家",
                        "url": "https://www.ithome.com/0/800/123.htm",
                    }
                ],
                "intl_consumer": [],
                "cn_consumer": [
                    {
                        "title": "咖啡品牌门店数破万",
                        "summary": "门店数量突破一万家。",
                        "source": "刀法研究所",
                        "url": "https://www.digitaling.com/articles/1234567.html",
                    }
                ],
            },
        },
        ensure_ascii=False,
    )

    # GitHub 部分的 mock：结构化四件套 + 一条编造仓库
    github_canned = json.dumps(
        {
            "digest_repos": "这批项目都在让 AI 助手更好用",
            "repos": [
                {
                    "repo": "example/cool-agent",
                    "url": "https://github.com/example/cool-agent",
                    "intro": "一句话就能搭起一个会自己干活的 AI 助手。",
                    "features": ["自动拆解任务", "支持多种模型"],
                    "guide": "需要先装 Node 18，然后一条命令跑起来。",
                    "install": "npx cool-agent init",
                },
                {
                    "repo": "evil/fake",
                    "url": "https://github.com/evil/fake",
                    "intro": "编造的",
                    "features": ["假的"],
                },
            ],
        },
        ensure_ascii=False,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            user_text = json.dumps(body, ensure_ascii=False)

            # 靠 system 提示词区分是新闻调用还是 GitHub 调用
            system = (body.get("messages") or [{}])[0].get("content") or ""
            is_github = "开源项目" in system or "技术布道者" in system
            calls.append({"is_github": is_github, "body": body, "user": user_text})

            content = github_canned if is_github else news_canned
            payload = {
                "id": "mock-1",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "deepseek-chat"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # 静音
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    original_key = config.DEEPSEEK_API_KEY
    original_base = config.DEEPSEEK_BASE_URL
    try:
        config.DEEPSEEK_API_KEY = "sk-selftest-dummy"
        config.DEEPSEEK_BASE_URL = f"http://127.0.0.1:{port}"
        result = summarizer.summarize(make_news(), make_repos(), dry_run=False)
    finally:
        config.DEEPSEEK_API_KEY = original_key
        config.DEEPSEEK_BASE_URL = original_base
        server.shutdown()
        server.server_close()

    news_calls = [c for c in calls if not c["is_github"]]
    gh_calls = [c for c in calls if c["is_github"]]
    by_key = {s["key"]: s["items"] for s in result["sections"]}

    check("新闻与 GitHub 各发起一次独立调用", len(news_calls) == 1 and len(gh_calls) == 1,
          f"新闻 {len(news_calls)} 次 / GitHub {len(gh_calls)} 次")
    check("两次调用都指定 json_object 输出",
          all(c["body"].get("response_format", {}).get("type") == "json_object" for c in calls))
    check("两次调用都带 system 提示词",
          all((c["body"].get("messages") or [{}])[0].get("role") == "system" for c in calls))

    news_user = news_calls[0]["user"] if news_calls else ""
    gh_user = gh_calls[0]["user"] if gh_calls else ""
    gh_system = (gh_calls[0]["body"]["messages"][0]["content"] if gh_calls else "")

    check("新闻提示词含四大板块", all(k in news_user for k in summarizer.NEWS_SECTION_KEYS))
    check("新闻提示词不再包含 github 板块", '"github"' not in news_user)
    check("新闻提示词含分类口径", "不设大公司专属板块" in news_user)
    check("GitHub 提示词含客观性约束", "禁止编造" in gh_system)
    check("GitHub 提示词要求大白话", "说人话" in gh_system)
    check("GitHub 提示词要求命令来自 README", "必须来自 README" in gh_system or "README 原文" in gh_system)
    check("GitHub 用户提示词含结构化字段要求",
          all(k in gh_user for k in ("intro", "features", "guide", "install")))
    check("README 摘录被送进 GitHub 提示词", "readme_excerpt" in gh_user)

    check("结果未经整体降级", not result.get("degraded", False), str(result.get("degraded_reason")))
    check("导读来自模型", result["digest"].startswith("OpenAI 发布新推理模型"))
    check("国际科技保留 1 条且剔除幻觉", len(by_key["intl_tech"]) == 1, str(len(by_key["intl_tech"])))
    check("国内科技 1 条", len(by_key["cn_tech"]) == 1)
    check("国内消费 1 条", len(by_key["cn_consumer"]) == 1)
    check("GitHub 保留 1 条且剔除编造仓库", len(by_key["github"]) == 1, str(len(by_key["github"])))
    check("GitHub 条目含 intro", bool(by_key["github"][0].get("intro")))
    check("GitHub 条目含 features", len(by_key["github"][0].get("features", [])) == 2)
    check("GitHub 条目含 install 命令", by_key["github"][0].get("install") == "npx cool-agent init")
    check("GitHub 星数取真实值 1234", by_key["github"][0].get("stars") == "1234")
    check("GitHub 板块带导语", bool(
        next(s for s in result["sections"] if s["key"] == "github").get("lead")))
    check("板块顺序与配置一致", [s["key"] for s in result["sections"]] == config.SECTION_KEYS)


def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """WCAG 2.1 相对对比度。"""

    def luminance(hex_color: str) -> float:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
        return (
            0.2126 * _srgb_to_linear(r)
            + 0.7152 * _srgb_to_linear(g)
            + 0.0722 * _srgb_to_linear(b)
        )

    l1, l2 = luminance(fg_hex), luminance(bg_hex)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def test_contrast() -> None:
    """从模板里读出 CSS 变量，逐对校验对比度（防止改配色时改出不可读的小字）。"""
    print("\n[12] 配色对比度（WCAG AA）")

    import re

    css = (config.BASE_DIR / "template.html").read_text(encoding="utf-8")
    block = css.split(":root {", 1)[1].split("}", 1)[0]

    def pick(name: str) -> str:
        m = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", block)
        return m.group(1) if m else ""

    # 纸质米色三档底板 —— 正文可能落在其中任意一档上
    backdrops = [("主底板", pick("paper")), ("次级底", pick("paper-alt")), ("悬停底", pick("paper-deep"))]

    for label, var in (
        ("正文 --ink", "ink"),
        ("次级 --ink-soft", "ink-soft"),
        ("辅助 --muted", "muted"),
        ("点缀 --accent（暗红）", "accent"),
        ("点缀 --accent-2（深绿）", "accent-2"),
    ):
        fg = pick(var)
        if not fg:
            check(f"{label} 变量可解析", False, f"fg={fg}")
            continue
        for bg_name, bg in backdrops:
            if not bg:
                check(f"{bg_name} 变量可解析", False, bg_name)
                continue
            ratio = contrast_ratio(fg, bg)
            check(
                f"{label} on {bg_name} ≥ 4.5:1（实测 {ratio:.2f}:1）",
                ratio >= 4.5,
                f"{fg} on {bg} = {ratio:.2f}:1",
            )

    # 反黑高亮（侧栏选中项 / Tab 选中态）上的文字也必须可读
    ink, paper = pick("ink"), pick("paper")
    if ink and paper:
        ratio = contrast_ratio(paper, ink)
        check(f"反黑块上的文字 ≥ 4.5:1（实测 {ratio:.2f}:1）", ratio >= 4.5)

    paper_accent = pick("accent")
    if paper_accent and paper:
        check(
            f"反白按钮底色上的纸色文字 ≥ 4.5:1（实测 {contrast_ratio(paper, paper_accent):.2f}:1）",
            contrast_ratio(paper, paper_accent) >= 4.5,
        )

    # 底板必须是纸质米色，不能是纯白
    check("主底板不是纯白", pick("paper").lower() != "#ffffff", pick("paper"))


def test_github_daily_selection() -> None:
    """3+2 混合推荐：过滤规则、状态持久化、轮播游标推进。全程离线。"""
    print("\n[13] GitHub 3+2 每日选品")

    import shutil
    import tempfile

    # ---- 1. 名称过滤规则 ----
    def make(
        name: str,
        stars: int = 9999,
        desc: str = "a tool",
        topics: list | None = None,
    ) -> fetcher.RepoItem:
        return fetcher.RepoItem(
            full_name=name,
            url=f"https://github.com/{name}",
            description=desc,
            stars=stars,
            # 默认给个 topic：现在零 topic 会被过滤掉（见下一个断言组）
            topics=["ai"] if topics is None else topics,
        )

    for bad in (
        "sindresorhus/awesome",
        "vinta/awesome-python",
        "EbookFoundation/free-programming-books",
        "public-apis/public-apis",
        "nilbuild/developer-roadmap",
        "microsoft/ML-For-Beginners",
        "rasbt/LLMs-from-scratch",
        "Developer-Y/cs-video-courses",
    ):
        check(f"排除清单/教程类：{bad.split('/')[-1]}", not fetcher._is_relevant_repo(make(bad)))

    for good in ("n8n-io/n8n", "excalidraw/excalidraw", "ShareX/ShareX", "httpie/cli"):
        check(f"保留真工具：{good.split('/')[-1]}", fetcher._is_relevant_repo(make(good)))

    check("星数过低被排除", not fetcher._is_relevant_repo(make("a/b", stars=1)))

    # ---- 零 topic 过滤（拦玩票/抗议型仓库）----
    # 实测依据：曾混进来 ai-sucks-butt/ai-sucks-butt（"觉得 AI 不行就点个星"），
    # 它有语言标记（Python）所以按语言拦不住，但零 topic；
    # 而同期 6 个真工具全都有 topic。
    check(
        "零 topic 仓库被排除",
        not fetcher._is_relevant_repo(make("ai-sucks-butt/ai-sucks-butt", topics=[])),
    )
    check(
        "有 topic 的同类仓库保留",
        fetcher._is_relevant_repo(make("MengTo/threeui", topics=["react", "threejs"])),
    )

    # 该规则可关闭
    original_require = config.GITHUB_REQUIRE_TOPICS
    try:
        config.GITHUB_REQUIRE_TOPICS = False
        check(
            "关闭后不再要求 topic",
            fetcher._is_relevant_repo(make("some/repo", topics=[])),
        )
    finally:
        config.GITHUB_REQUIRE_TOPICS = original_require

    # ---- 2. 状态文件读写与容错 ----
    original_state = config.GITHUB_STATE_FILE
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="gh-selftest-"))
    try:
        config.GITHUB_STATE_FILE = tmp / "github_offset.json"

        fresh = fetcher.load_github_state()
        check("无状态文件时返回初始状态", fresh["classic_offset"] == 0 and fresh["classic_pool"] == [])

        fresh["classic_offset"] = 4
        fresh["recent_trending"] = ["a/b", "c/d"]
        fetcher.save_github_state(fresh)
        check("状态文件已写入", config.GITHUB_STATE_FILE.exists())

        reloaded = fetcher.load_github_state()
        check("游标被正确读回", reloaded["classic_offset"] == 4, str(reloaded["classic_offset"]))
        check("记忆列表被正确读回", reloaded["recent_trending"] == ["a/b", "c/d"])

        config.GITHUB_STATE_FILE.write_text("{ 这不是 JSON", encoding="utf-8")
        broken = fetcher.load_github_state()
        check("状态文件损坏时安全重置", broken["classic_offset"] == 0 and broken["classic_pool"] == [])

        config.GITHUB_STATE_FILE.write_text('{"classic_offset": "坏值", "recent_trending": 123}',
                                            encoding="utf-8")
        dirty = fetcher.load_github_state()
        check("字段类型异常时退回默认值",
              dirty["classic_offset"] == 0 and dirty["recent_trending"] == [])

        # ---- 3. 轮播游标推进（打桩，不走网络）----
        fake_pool = [f"owner{i}/repo{i}" for i in range(1, 11)]  # 10 个，每天取 2 → 5 天一轮

        def fake_build(session):  # noqa: ANN001
            return list(fake_pool)

        def fake_fetch(session, names):  # noqa: ANN001
            return [
                fetcher.RepoItem(full_name=n, url=f"https://github.com/{n}", stars=1000)
                for n in names
            ]

        original_build = fetcher._build_classic_pool
        original_fetch = fetcher._fetch_repos_by_name
        fetcher._build_classic_pool = fake_build
        fetcher._fetch_repos_by_name = fake_fetch
        try:
            config.GITHUB_STATE_FILE = tmp / "rotation.json"
            state = fetcher.load_github_state()

            seen_round: list = []
            for day in range(1, 7):
                picked = fetcher.select_classic_repos(None, state)
                seen_round.append([r.full_name for r in picked])
                fetcher.save_github_state(state)

            check("第 1 天取池首两个", seen_round[0] == ["owner1/repo1", "owner2/repo2"], str(seen_round[0]))
            check("第 2 天推进到 3-4", seen_round[1] == ["owner3/repo3", "owner4/repo4"], str(seen_round[1]))
            check("第 3 天推进到 5-6", seen_round[2] == ["owner5/repo5", "owner6/repo6"], str(seen_round[2]))

            first_cycle = [n for day in seen_round[:5] for n in day]
            check("一轮 5 天恰好覆盖池中 10 个且不重复",
                  len(first_cycle) == 10 and len(set(first_cycle)) == 10, str(len(set(first_cycle))))

            check("第 6 天绕回池首", seen_round[5] == ["owner1/repo1", "owner2/repo2"], str(seen_round[5]))
            check("记录了完成的轮次", state.get("classic_cycle", 0) >= 1, str(state.get("classic_cycle")))
            check("池子被持久化进状态文件",
                  state.get("classic_pool") == fake_pool, str(len(state.get("classic_pool", []))))
        finally:
            fetcher._build_classic_pool = original_build
            fetcher._fetch_repos_by_name = original_fetch
    finally:
        config.GITHUB_STATE_FILE = original_state
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 4. 配置一致性 ----
    check(
        "板块条数 = 趋势数 + 经典数",
        config.GITHUB_DISPLAY_COUNT == config.GITHUB_TREND_COUNT + config.GITHUB_CLASSIC_COUNT,
        f"{config.GITHUB_DISPLAY_COUNT} = {config.GITHUB_TREND_COUNT} + {config.GITHUB_CLASSIC_COUNT}",
    )
    check("经典池推进步长等于经典条数", config.GITHUB_CLASSIC_COUNT == 2)


def test_archive_scanner() -> None:
    """存档扫描必须能忽略 github_offset.json 这类状态文件（否则会反复告警）。"""
    print("\n[14] 存档扫描与状态文件共存")

    import shutil
    import tempfile

    original = (config.ARCHIVE_DIR, config.DIST_DIR, config.OUTPUT_HTML, config.HISTORY_MANIFEST)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="digest-scan-"))
    try:
        config.ARCHIVE_DIR = tmp / "archive"
        config.DIST_DIR = tmp / "dist"
        config.OUTPUT_HTML = config.DIST_DIR / "index.html"
        config.HISTORY_MANIFEST = config.DIST_DIR / "history.json"
        config.ARCHIVE_DIR.mkdir(parents=True)

        rep = summarizer.build_fallback_result(make_news(), make_repos(), summarizer.today_local(), "自检")
        renderer.save_archive(rep, STATS)

        # 混入状态文件与其它非存档 JSON
        (config.ARCHIVE_DIR / "github_offset.json").write_text(
            json.dumps({"classic_offset": 2, "classic_pool": ["a/b"]}), encoding="utf-8"
        )
        (config.ARCHIVE_DIR / "something_else.json").write_text("{}", encoding="utf-8")

        entries = renderer.load_archive()
        check("只识别日期命名的存档", len(entries) == 1, f"{len(entries)} 条")
        check("状态文件未被当成存档", all(e["date"] != "github_offset" for e in entries))

        result = renderer.render_site(rep, STATS)
        check("渲染不受状态文件影响", result["archives_rendered"] == 1, str(result["archives_rendered"]))

        page = (config.DIST_DIR / "archive" / f"{rep['date']}.html").read_text(encoding="utf-8")
        check("往期页正常生成", "classic_pool" not in page and "每日早报" in page)
    finally:
        config.ARCHIVE_DIR, config.DIST_DIR, config.OUTPUT_HTML, config.HISTORY_MANIFEST = original
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 62)
    print("每日早报 · 离线自检")
    print("=" * 62)

    test_text_utils()
    test_url_guard()
    test_json_parsing()
    test_normalize_result()
    test_github_structure()
    test_fallback()
    test_notifier()
    test_render()
    test_language_modes()
    test_history()
    test_contrast()
    test_deepseek_pipeline()
    test_github_daily_selection()
    test_archive_scanner()

    print("\n" + "=" * 62)
    print(f"结果：{PASSED} 项通过，{FAILED} 项失败")
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
