#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
每日早报主流程。

    RSS + GitHub 采集  →  DeepSeek 提炼  →  HTML 渲染  →  邮件推送

用法：
    python main.py                 # 完整流程
    python main.py --dry-run       # 跳过 AI 与推送，用原始素材渲染页面（本地调试用）
    python main.py --no-notify     # 跑完整流程但不推送
    python main.py --force         # 忽略"今天已生成过"的幂等闸门，强制重跑
    python main.py --skip-fetch-cache  # 忽略 _raw.json 缓存，强制重新采集
    python main.py --serve         # 渲染后起一个本地 HTTP 服务预览

退出码：
    0  正常完成（含命中幂等闸门而跳过的情况）
    1  致命错误（页面都没能生成）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
import fetcher
import notifier
import renderer
import summarizer


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)-11s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # 第三方库的日志太吵，压到 WARNING
    for noisy in ("urllib3", "requests", "openai", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger("main")


# --------------------------------------------------------------------------
# 各阶段包装：任何一步失败都要有明确日志，且不阻断后续可降级的步骤
# --------------------------------------------------------------------------
def stage_fetch() -> tuple[
    List[fetcher.NewsItem],
    List[fetcher.RepoItem],
    Dict[str, str],
    Dict[str, Any],
]:
    """采集阶段。新闻与 GitHub 分别容错。返回 (新闻, 仓库, 诊断文本, 结构化统计)。"""
    diagnostics = {"news": "—", "github": "—"}
    stats: Dict[str, Any] = {
        "rss_sources": len([s for s in config.RSS_SOURCES if s.get("enabled")]),
        "rss_ok": 0,
        "rss_failed": 0,
        "raw_news": 0,
        "github_queries": 0,
    }

    news: List[fetcher.NewsItem] = []
    repos: List[fetcher.RepoItem] = []

    session = fetcher.build_session()

    try:
        news, news_report = fetcher.fetch_all_news(session)
        diagnostics["news"] = news_report.summary
        stats["rss_ok"] = news_report.ok
        stats["rss_failed"] = news_report.failed
        stats["raw_news"] = len(news)
    except Exception as exc:  # noqa: BLE001
        logger.error("RSS 采集整体失败：%s", exc, exc_info=True)
        diagnostics["news"] = f"整体失败：{type(exc).__name__}"
        stats["rss_failed"] = stats["rss_sources"]

    try:
        repos, repo_report = fetcher.fetch_github_daily(session)
        diagnostics["github"] = repo_report.summary
        stats["github_queries"] = repo_report.ok
    except fetcher.GitHubAuthError as exc:
        # 认证问题要给出可操作的提示，而不是一串堆栈
        logger.error("GitHub 认证失败：%s", exc)
        diagnostics["github"] = "整体失败：缺少或无效的 GITHUB_TOKEN"
    except fetcher.GitHubRateLimitError as exc:
        logger.error("GitHub 触发限流：%s", exc)
        diagnostics["github"] = f"整体失败：限流（{exc}）"
    except Exception as exc:  # noqa: BLE001
        logger.error("GitHub 采集整体失败：%s", exc, exc_info=True)
        diagnostics["github"] = f"整体失败：{type(exc).__name__}"

    logger.info("采集阶段结束：新闻 %d 条，开源项目 %d 个", len(news), len(repos))
    return news, repos, diagnostics, stats


def stage_summarize(
    news: List[fetcher.NewsItem],
    repos: List[fetcher.RepoItem],
    dry_run: bool,
) -> Dict[str, Any]:
    """提炼阶段。summarizer 内部已做降级，这里再兜一层。"""
    try:
        return summarizer.summarize(news, repos, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("提炼阶段异常，强制降级：%s", exc, exc_info=True)
        return summarizer.build_fallback_result(
            news, repos, summarizer.today_local(), f"{type(exc).__name__}: {exc}"
        )


def stage_render(
    report: Dict[str, Any],
    stats: Dict[str, Any],
    *,
    write_archive: bool = True,
    dry_run: bool = False,
) -> bool:
    """
    渲染阶段：写入当天存档并渲染整站（今日页 + 全部往期页）。失败即致命。

    write_archive=False 用于幂等跳过路径：那种情况 report 本来就是从存档里读出来的，
    再写一次只会刷新 generated_at，在 Actions 里就会多出一个无意义的提交。
    """
    try:
        if write_archive:
            renderer.save_archive(report, stats, dry_run=dry_run)
        result = renderer.render_site(report, stats)
        logger.info(
            "整站渲染完成：往期页面 %d 期，历史共 %d 期",
            result["archives_rendered"],
            result["history_total"],
        )
        stats["archives_rendered"] = result["archives_rendered"]
        stats["history_total"] = result["history_total"]
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("页面渲染失败：%s", exc, exc_info=True)
        return False


def guard_already_done(
    today: str,
    force: bool,
) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    幂等闸门：当天已经出过正式早报就不要再跑一遍。

    为什么必须有这道闸门：改成外部定时器（cron-job.org 等）触发之后，同一天被触发
    多次是很正常的事——在控制台点一次 "Test Run"、任务失败后重试、手动 Re-run，
    都会再打一次 workflow_dispatch 接口。没有闸门时每次重复触发都会：
      · 再发一封一模一样的邮件
      · 再调一次 DeepSeek（白烧额度）
      · 用新的 generated_at 覆盖当天存档，产生无意义的 git 提交

    返回 (report, stats) 表示"今天已完成，应当跳过"；返回 None 表示正常往下执行。
    dry-run 产物不算数，否则本地预览一次就会挡住当天的正式运行。
    """
    if force:
        logger.info("指定了 --force，跳过当日幂等检查")
        return None

    payload = renderer.load_archive_entry(today)
    if payload is None:
        return None

    if payload.get("dry_run"):
        logger.info("当日存档是 dry-run 预览产物，不作为已完成依据，继续正常执行")
        return None

    report = payload["report"]
    stats = payload.get("stats") or {}

    logger.warning("=" * 68)
    logger.warning("幂等闸门：%s 的早报已存在，跳过采集 / AI 提炼 / 邮件推送", today)
    logger.warning("存档生成于 %s", payload.get("generated_at", "未知时间"))
    logger.warning("如需强制重跑（例如今天的版面有问题要重出一期）：加 --force")
    logger.warning("=" * 68)
    return report, stats


def stage_notify(report: Dict[str, Any], no_notify: bool, dry_run: bool) -> None:
    """通知阶段。失败只告警，不影响已生成的页面。"""
    if no_notify:
        logger.info("指定了 --no-notify，跳过邮件发送")
        return

    try:
        ok = notifier.send_email(report, dry_run=dry_run)
        if not ok:
            logger.warning("邮件推送未成功，但页面已正常生成")
    except Exception as exc:  # noqa: BLE001
        logger.error("推送阶段异常：%s", exc, exc_info=True)


# --------------------------------------------------------------------------
# 调试辅助
# --------------------------------------------------------------------------
def dump_raw(report: Dict[str, Any], diagnostics: Dict[str, str], stats: Dict[str, Any]) -> None:
    """把本次产物快照写到 dist/_raw.json，方便线上排查。"""
    try:
        config.DIST_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stats": stats,
            "diagnostics": diagnostics,
            "report": report,
        }
        config.RAW_DUMP_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入调试快照失败：%s", exc)


def serve_preview(port: int = 8000) -> None:
    """本地预览：起个静态服务，方便手机连同一局域网查看响应式效果。"""
    import functools
    import http.server
    import socketserver

    class Handler(http.server.SimpleHTTPRequestHandler):
        # 显式声明 UTF-8：默认实现只发 "text/html" 不带 charset，
        # 浏览器一旦猜错编码，整页中文都会变成乱码。
        extensions_map = {
            **http.server.SimpleHTTPRequestHandler.extensions_map,
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, fmt, *args):
            logger.debug("preview %s", fmt % args)

    handler = functools.partial(Handler, directory=str(config.DIST_DIR))

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), handler) as httpd:
        logger.info("预览地址：http://localhost:%d/（Ctrl+C 退出）", port)
        logger.info("手机预览：http://<本机局域网IP>:%d/", port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("预览服务已停止")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取科技/消费新闻与 GitHub 热门项目，生成每日早报网页并推送邮件。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="跳过 DeepSeek 与邮件推送，用原始素材渲染页面",
    )
    parser.add_argument("--no-notify", action="store_true", help="不发送邮件")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使当天已经生成过，也强制重新采集并再出一期",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--serve", action="store_true", help="渲染后启动本地预览服务")
    parser.add_argument("--port", type=int, default=8000, help="预览服务端口（默认 8000）")
    parser.add_argument("--keep-dist", action="store_true", help="渲染前不清理 dist 目录")
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    started = datetime.now(timezone.utc)

    logger.info("=" * 68)
    logger.info("每日早报流水线启动")
    logger.info("时区 %s | 当前 %s", config.TIMEZONE, summarizer.local_now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("模式 %s", "DRY-RUN" if args.dry_run else "正式")
    logger.info("=" * 68)

    if not config.DEEPSEEK_API_KEY and not args.dry_run:
        logger.warning("未检测到 DEEPSEEK_API_KEY，AI 提炼将走降级流程")

    if not config.GITHUB_TOKEN:
        logger.warning(
            "未检测到 GITHUB_TOKEN：GitHub Search API 将按匿名身份调用（10 次/分钟），"
            "极易触发限流导致开源项目板块缺失"
        )

    if not args.dry_run and not args.no_notify:
        if not (config.SMTP_USER and config.SMTP_PASS):
            logger.warning("未检测到 SMTP_USER / SMTP_PASS，邮件推送将被跳过")
        else:
            logger.info("邮件收件人：%s", "、".join(config.resolve_recipients()) or "(未配置)")

    # ---- 0. 幂等闸门 ----
    # 外部定时器（cron-job.org 等）重复触发同一天是常态：点一次 Test Run、
    # 失败后重试、手动 Re-run 都会再打一次 dispatch 接口。这里先挡一道。
    already_done = guard_already_done(summarizer.today_local().isoformat(), args.force)

    if already_done is not None:
        report, stats = already_done
        # 不重新采集、不重新提炼：只把整站按既有存档重渲染一遍。
        # dist/ 必须存在，否则后面的 gh-pages 部署步骤会因为目录缺失而失败。
        diagnostics = {
            "news": "命中幂等闸门，沿用当日存档，未重新采集",
            "github": "命中幂等闸门，沿用当日存档，未重新采集",
        }
        stats["skipped"] = True

        if not args.keep_dist:
            renderer.cleanup_dist()
        if not stage_render(report, stats, write_archive=False):
            return 1

        dump_raw(report, diagnostics, stats)
        logger.info("本次未重复发送邮件；线上页面已按既有存档重新渲染。")

        if args.serve:
            serve_preview(args.port)
        return 0

    # ---- 1. 采集 ----
    news, repos, diagnostics, stats = stage_fetch()

    if not news and not repos:
        logger.error("新闻与 GitHub 均未采集到任何内容，终止流程")
        return 1

    # ---- 2. 提炼 ----
    report = stage_summarize(news, repos, args.dry_run)

    # ---- 3. 渲染 ----
    if not args.keep_dist:
        renderer.cleanup_dist()
    if not stage_render(report, stats, dry_run=args.dry_run):
        return 1

    dump_raw(report, diagnostics, stats)

    # ---- 4. 通知 ----
    stage_notify(report, args.no_notify, args.dry_run)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    total_items = sum(len(sec["items"]) for sec in report["sections"])

    logger.info("=" * 68)
    logger.info("完成：%d 条内容，耗时 %.1fs", total_items, elapsed)
    logger.info("今日页面：%s", config.OUTPUT_HTML)
    logger.info("往期存档：%s（共 %s 期）", config.ARCHIVE_DIR, stats.get("history_total", 0))
    logger.info("线上地址（部署后）：%s", config.resolve_site_url())
    logger.info("=" * 68)

    if args.serve:
        serve_preview(args.port)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:  # noqa: BLE001 —— 兜底，保证 Actions 里能看到完整堆栈
        logging.getLogger("main").critical("未捕获异常", exc_info=True)
        sys.exit(1)
