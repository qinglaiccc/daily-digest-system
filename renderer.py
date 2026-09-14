# -*- coding: utf-8 -*-
"""
渲染层。

产出物（全部落在 dist/，由 gh-pages 发布）：
    dist/index.html                今天的早报
    dist/archive/YYYY-MM-DD.html   往期早报（由 archive/*.json 重新渲染）
    dist/archive/index.html        往期索引
    dist/history.json              往期清单（供前端读取）

唯一数据源是仓库根目录的 archive/YYYY-MM-DD.json。每次运行都由它重新渲染全部往期页面，
所以模板升级后往期页面也会跟着更新，不会留下一堆旧样式的死页面。
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

import config
from summarizer import format_date_cn, local_now

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 过滤器与工具
# --------------------------------------------------------------------------
def _format_stars(value: Any) -> str:
    """把星数格式化成 1.2k / 23.4k 这种易读形式。"""
    if value in (None, ""):
        return ""
    text = str(value).strip().lower().replace("stars", "").replace("★", "").strip()
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]

    text = text.replace(",", "").replace("+", "")
    try:
        number = float(text) * multiplier
    except ValueError:
        return str(value)

    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}m".replace(".0m", "m")
    if number >= 1_000:
        return f"{number / 1_000:.1f}k".replace(".0k", "k")
    return str(int(number))


def _build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(config.BASE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["starnum"] = _format_stars
    return env


def _count_media(report: Dict[str, Any]) -> int:
    media = set()
    for section in report.get("sections", []):
        for item in section.get("items", []):
            if item.get("source"):
                media.add(item["source"])
    return len(media)


def _total_items(report: Dict[str, Any]) -> int:
    return sum(len(sec.get("items", [])) for sec in report.get("sections", []))


def _repo_count(report: Dict[str, Any]) -> int:
    for section in report.get("sections", []):
        if section.get("key") == "github":
            return len(section.get("items", []))
    return 0


# --------------------------------------------------------------------------
# 存档读写
# --------------------------------------------------------------------------
def archive_path(for_date: str) -> Path:
    return config.ARCHIVE_DIR / f"{for_date}.json"


def save_archive(report: Dict[str, Any], stats: Dict[str, Any]) -> Path:
    """把当天的报告落成 archive/YYYY-MM-DD.json（会被提交回仓库）。"""
    config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    target = archive_path(report["date"])

    payload = {
        "date": report["date"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "report": report,
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("已写入往期存档：%s", target)
    return target


def load_archive() -> List[Dict[str, Any]]:
    """
    读取全部往期存档，按日期倒序返回。

    单份存档损坏不应影响整站生成，因此逐个容错跳过。
    """
    if not config.ARCHIVE_DIR.exists():
        return []

    entries: List[Dict[str, Any]] = []
    for path in sorted(config.ARCHIVE_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("跳过损坏的存档 %s：%s", path.name, exc)
            continue

        report = data.get("report")
        if not isinstance(report, dict) or not report.get("date"):
            logger.warning("跳过结构异常的存档 %s", path.name)
            continue

        entries.append(
            {
                "date": report["date"],
                "date_cn": format_date_cn(date.fromisoformat(report["date"])),
                "digest": report.get("digest", ""),
                "count": _total_items(report),
                "degraded": bool(report.get("degraded")),
                "report": report,
                "stats": data.get("stats", {}),
            }
        )

    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _base_context(
    report: Dict[str, Any],
    stats: Dict[str, Any],
    history: List[Dict[str, Any]],
    root: str,
    current_date: str,
) -> Dict[str, Any]:
    """构造模板上下文。root 是到站点根目录的相对前缀（"" 或 "../"）。"""
    report_date = date.fromisoformat(report["date"])
    total = _total_items(report)
    repos = _repo_count(report)

    # 往期列表里排除"当前正在看的这一天"
    past = [h for h in history if h["date"] != current_date][: config.HISTORY_MAX_DAYS]
    prev_issue = next((h for h in history if h["date"] < current_date), None)
    next_issue = next((h for h in reversed(history) if h["date"] > current_date), None)

    return {
        "report": {**report, "date_cn": format_date_cn(report_date)},
        "day": report_date.strftime("%d"),
        "year": report_date.strftime("%Y"),
        "month": report_date.strftime("%m"),
        "meta": {
            "generated_at": local_now().strftime("%Y-%m-%d %H:%M"),
            "news_count": total - repos,
            "repo_count": repos,
            "total_count": total,
            "media_count": _count_media(report),
            "rss_sources": stats.get("rss_sources", 0),
            "rss_ok": stats.get("rss_ok", 0),
            "rss_failed": stats.get("rss_failed", 0),
            "raw_news": stats.get("raw_news", 0),
            "github_queries": stats.get("github_queries", 0),
            "site_url": config.resolve_site_url(),
        },
        "history": history[: config.HISTORY_MAX_DAYS],
        "past": past,
        "prev_issue": prev_issue,
        "next_issue": next_issue,
        "root": root,
        "today_url": f"{root}index.html",
        "archive_index_url": f"{root}archive/index.html" if root else "archive/index.html",
        "is_archive": root != "",
    }


def render_issue(
    report: Dict[str, Any],
    stats: Dict[str, Any],
    history: List[Dict[str, Any]],
    output_path: Path,
    root: str = "",
) -> Path:
    """渲染一期早报。root="" 用于今日页，root="../" 用于往期页。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ctx = _base_context(report, stats, history, root, report["date"])
    env = _build_environment()
    html = env.get_template(config.TEMPLATE_PATH.name).render(**ctx)
    output_path.write_text(html, encoding="utf-8")

    logger.info("渲染：%s（%.1f KB）", output_path, output_path.stat().st_size / 1024)
    return output_path


def render_archive_index(
    history: List[Dict[str, Any]],
    output_path: Path,
) -> Optional[Path]:
    """渲染往期索引页。没有往期时返回 None。"""
    if not history:
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)

    env = _build_environment()
    template = env.get_template(config.TEMPLATE_INDEX_PATH.name)
    html = template.render(
        issues=history[: config.HISTORY_MAX_DAYS],
        total=len(history),
        generated_at=local_now().strftime("%Y-%m-%d %H:%M"),
        site_url=config.resolve_site_url(),
        root="../",
    )
    output_path.write_text(html, encoding="utf-8")
    logger.info("渲染往期索引：%s", output_path)
    return output_path


def render_site(report: Dict[str, Any], stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    渲染整站：今日页 + 全部往期页 + 往期索引 + history.json。

    返回一个摘要，供日志与调试快照使用。
    """
    history = load_archive()

    # 今日页
    render_issue(report, stats, history, config.OUTPUT_HTML, root="")

    # 往期页：每份存档都重新渲染一次，保证模板升级后往期页面同步更新
    rendered_archives = 0
    for entry in history[: config.HISTORY_MAX_DAYS]:
        target = config.DIST_DIR / "archive" / f"{entry['date']}.html"
        render_issue(
            entry["report"],
            entry.get("stats", {}),
            history,
            target,
            root="../",
        )
        rendered_archives += 1

    render_archive_index(history, config.DIST_DIR / "archive" / "index.html")

    # history.json：给前端或外部工具读取的清单
    config.HISTORY_MANIFEST.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "latest": report["date"],
                "issues": [
                    {
                        "date": e["date"],
                        "date_cn": e["date_cn"],
                        "digest": e["digest"],
                        "count": e["count"],
                        "url": f"archive/{e['date']}.html",
                    }
                    for e in history[: config.HISTORY_MAX_DAYS]
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # GitHub Pages 默认走 Jekyll，加 .nojekyll 避免下划线开头的文件被忽略
    (config.DIST_DIR / ".nojekyll").write_text("", encoding="utf-8")

    return {
        "archives_rendered": rendered_archives,
        "history_total": len(history),
    }


def cleanup_dist(keep_raw_dump: bool = False) -> None:
    """清理上一次的产物，避免陈旧文件被一起发布。archive/ 是数据源，不动。"""
    if not config.DIST_DIR.exists():
        return

    for child in config.DIST_DIR.iterdir():
        if keep_raw_dump and child.name == "_raw.json":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


__all__ = [
    "render_issue",
    "render_site",
    "render_archive_index",
    "save_archive",
    "load_archive",
    "archive_path",
    "cleanup_dist",
]
