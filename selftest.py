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
4. normalize_result 的字段收敛与条数限制
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

    result = summarizer.normalize_result(raw, guard, today)
    by_key = {s["key"]: s["items"] for s in result["sections"]}

    check("五个板块齐全", len(result["sections"]) == 5, str(len(result["sections"])))
    check(
        f"条数被限制在 {config.ITEMS_PER_SECTION} 条内",
        len(by_key["intl_tech"]) == config.ITEMS_PER_SECTION,
        str(len(by_key["intl_tech"])),
    )
    check(
        "幻觉链接被剔除",
        all("fake-news-site" not in i["url"] for i in by_key["intl_tech"]),
    )
    check("类型错误的板块被容错为空列表", by_key["cn_tech"] == [])
    check("GitHub 板块保留 stars 字段", by_key["github"][0].get("stars") == "1234")
    check("导读被保留", result["digest"] == "今日导读")


def test_fallback() -> None:
    print("\n[5] 降级方案")

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


def test_notifier() -> None:
    print("\n[6] 邮件推送（SMTP）")

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
    print("\n[7] 页面渲染（Swiss 风格 token）")

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
        out = renderer.render_issue(report, STATS, [], config.OUTPUT_HTML, root="")
        html = out.read_text(encoding="utf-8")

        check("文件已生成", out.exists() and out.stat().st_size > 5000, f"{out.stat().st_size} 字节")
        check("无未渲染的 Jinja 变量", "{{" not in html and "{%" not in html)
        check("声明了 UTF-8", 'charset="UTF-8"' in html)
        check("包含 viewport 元信息（移动端适配）", "viewport" in html)
        check("包含五大板块锚点", all(f'id="{k}"' in html for k in config.SECTION_KEYS))
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

        # ---- Swiss 风格 token 合规（见 ui-style-swiss-minimal）----
        css = html.split("</style>")[0].lower()

        allowed = {"#e4002b", "#ffffff", "#f7f7f8", "#111111", "#444444", "#6b6b6b", "#e0e0e0"}
        found = {"#" + h for h in re.findall(r"#([0-9a-f]{6})\b", css)}
        stray = found - allowed

        check("只用白/灰阶 + 瑞士红", not stray, f"越界色值 {sorted(stray)}")
        check("点缀色确为 Swiss Red", "#e4002b" in css)
        check("无圆角", "border-radius" not in css)
        check("无阴影", "box-shadow" not in css)
        check("无渐变", "gradient" not in css)
        check("有 1px 发丝线栅格", "grid-lines" in css and "width: 1px" in css)
        check("使用单一无衬线字族", '"helvetica neue", helvetica, arial' in css)
        # 注意排除 "sans-serif" 本身：只有真正引入衬线字族才算破功
        # 这里必须是后顾断言 (?<!sans-)，否则会把 sans-serif 里的 serif 也匹配上
        check(
            "无衬线标题（未引入衬线字体）",
            not re.search(r"georgia|times new roman|(?<!sans-)serif|songti|source han serif", css),
        )
        check("数字等宽对齐", "tabular-nums" in css)
        check("无 emoji 装饰", not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", html))
        check("未用 Unicode 符号当图标", not any(g in html for g in ("▣", "◊", "↗", "★")))
        check("差异化动作：悬停显影栅格", "body:has(.item:hover) .grid-lines" in css)
        check("阅读全文为 CSS 绘制标记", 'class="ext"' in html and "阅读全文" in html)
    finally:
        config.DIST_DIR, config.OUTPUT_HTML = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_language_modes() -> None:
    print("\n[8] 中英对照（语言切换）")

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
    result = summarizer.normalize_result(raw, guard, summarizer.today_local())
    item = next(s for s in result["sections"] if s["key"] == "intl_tech")["items"][0]

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
    res_en = summarizer.normalize_result(raw_en, guard_en, summarizer.today_local())
    item_en = next(s for s in res_en["sections"] if s["key"] == "intl_tech")["items"][0]
    check("英文源判定为外文", item_en["is_foreign"] is True)
    check("英文原文标题被保留", item_en["title_original"].startswith("OpenAI ships"))


def test_history() -> None:
    print("\n[9] 往期存档")

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
    print("\n[10] DeepSeek 调用链（本地 mock 接口）")

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    captured: dict = {}

    # mock 返回：三个真实链接 + 一个编造链接（应被过滤）
    model_sections = {
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
        "github": [
            {
                "title": "example/cool-agent",
                "summary": "一个 AI Agent 框架。",
                "source": "GitHub",
                "url": "https://github.com/example/cool-agent",
                "stars": "1234",
                "language": "Python",
            }
        ],
    }
    canned = json.dumps(
        {
            "date": summarizer.today_local().isoformat(),
            "digest": "OpenAI 发布新推理模型；国内厂商发布 3nm 芯片。",
            "sections": model_sections,
        },
        ensure_ascii=False,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            captured["path"] = self.path
            captured["body"] = body

            payload = {
                "id": "mock-1",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "deepseek-chat"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": canned},
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

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

    body = captured.get("body", {})
    by_key = {s["key"]: s["items"] for s in result["sections"]}

    check("SDK 请求打到了 /chat/completions", captured.get("path") == "/chat/completions", str(captured.get("path")))
    check("请求指定了 json_object 输出", body.get("response_format", {}).get("type") == "json_object")
    check("请求包含 system 提示词", body.get("messages", [{}])[0].get("role") == "system")
    check("system 提示词包含客观性约束", "禁止主观评论" in (body.get("messages", [{}])[0].get("content") or ""))
    check("用户提示词包含五大板块要求", all(k in json.dumps(body, ensure_ascii=False) for k in config.SECTION_KEYS))
    check("用户提示词含分类口径要求", "不设大公司专属板块" in json.dumps(body, ensure_ascii=False))
    check("结果未经降级", not result.get("degraded", False), str(result.get("degraded_reason")))
    check("导读来自模型", result["digest"].startswith("OpenAI 发布新推理模型"))
    check("国际科技板块保留 1 条且剔除幻觉", len(by_key["intl_tech"]) == 1, str(len(by_key["intl_tech"])))
    check("国内科技板块 1 条", len(by_key["cn_tech"]) == 1)
    check("国内消费板块 1 条", len(by_key["cn_consumer"]) == 1)
    check("GitHub 板块 1 条", len(by_key["github"]) == 1)
    check("GitHub 条目保留星数", by_key["github"][0].get("stars") == "1234")
    # 注意：模型的 title 被改写成中文时，应通过标题回查补全来源名
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
    print("\n[11] 配色对比度（WCAG AA）")

    import re

    css = (config.BASE_DIR / "template.html").read_text(encoding="utf-8")
    block = css.split(":root {", 1)[1].split("}", 1)[0]

    def pick(name: str) -> str:
        m = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", block)
        return m.group(1) if m else ""

    paper = pick("paper")
    paper_alt = pick("paper-alt")

    for label, var in (
        ("正文 --ink", "ink"),
        ("次级 --ink-soft", "ink-soft"),
        ("辅助 --muted", "muted"),
        ("点缀 --accent", "accent"),
    ):
        fg = pick(var)
        if not (fg and paper and paper_alt):
            check(f"{label} 变量可解析", False, f"fg={fg} paper={paper}")
            continue
        for bg_name, bg in (("页面底色", paper), ("浅灰底", paper_alt)):
            ratio = contrast_ratio(fg, bg)
            check(
                f"{label} on {bg_name} ≥ 4.5:1（实测 {ratio:.2f}:1）",
                ratio >= 4.5,
                f"{fg} on {bg} = {ratio:.2f}:1",
            )

    # Swiss 风格只允许一个点缀色
    accents = re.findall(r"--accent:\s*(#[0-9a-fA-F]{6})", css)
    check("只定义了一个点缀色", len(accents) == 1, str(accents))


def main() -> int:
    print("=" * 62)
    print("每日早报 · 离线自检")
    print("=" * 62)

    test_text_utils()
    test_url_guard()
    test_json_parsing()
    test_normalize_result()
    test_fallback()
    test_notifier()
    test_render()
    test_language_modes()
    test_history()
    test_contrast()
    test_deepseek_pipeline()

    print("\n" + "=" * 62)
    print(f"结果：{PASSED} 项通过，{FAILED} 项失败")
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
