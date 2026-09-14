# -*- coding: utf-8 -*-
"""
通知层：把早报以 HTML 邮件形式推送。

只用 Python 标准库：
    smtplib          —— SMTP 会话（465 走 SMTP_SSL，587 走 STARTTLS）
    email.mime.*     —— 组装 multipart/alternative（纯文本 + HTML 双版本）

为什么邮件正文要单独写一套样式，而不是直接复用网页的 HTML：
邮件客户端（Gmail / QQ 邮箱 / Outlook）普遍不支持 flex、grid、CSS 变量、外部样式表，
部分客户端还会剥掉 <style> 标签。所以这里用「表格布局 + 行内样式」重写了一份精简版，
确保在主流邮箱里都能正常显示。

发送失败不会让整条流水线失败，所有异常都在这一层被吞掉并记录。
"""

from __future__ import annotations

import logging
import re
import smtplib
import time
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr, formatdate, make_msgid
from typing import Any, Dict, List, Optional

import config
from summarizer import format_date_cn

logger = logging.getLogger(__name__)

# 邮件里使用的配色，与网页的 Swiss 风格保持一致
_INK = "#111111"
_INK_SOFT = "#444444"
_MUTED = "#6b6b6b"
_RULE = "#e0e0e0"
_ACCENT = "#e4002b"
_PAPER = "#ffffff"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _plain(text: str, limit: int = 0) -> str:
    """去掉 HTML 标签并压缩空白，用于纯文本版本与主题行。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _esc(text: str) -> str:
    """HTML 转义，防止标题里的 & < > 破坏邮件结构。"""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _total_items(report: Dict[str, Any]) -> int:
    return sum(len(sec.get("items", [])) for sec in report.get("sections", []))


# --------------------------------------------------------------------------
# 主题
# --------------------------------------------------------------------------
def build_subject(report: Dict[str, Any]) -> str:
    """邮件主题：日期 + 导读摘要，超长按字符截断。"""
    report_date = date.fromisoformat(report["date"])
    subject = f"每日早报 · {report_date.strftime('%m月%d日')}"

    digest = _plain(report.get("digest", ""))
    if digest:
        subject = f"{subject} | {digest}"

    if report.get("degraded"):
        subject = f"[降级] {subject}"

    if len(subject) > config.EMAIL_SUBJECT_MAX:
        subject = subject[: config.EMAIL_SUBJECT_MAX - 1].rstrip() + "…"

    return subject


# --------------------------------------------------------------------------
# 纯文本版本
# --------------------------------------------------------------------------
def build_plain_text(report: Dict[str, Any], site_url: str) -> str:
    """纯文本正文。给不支持 HTML 的客户端兜底，也便于全文检索。"""
    report_date = date.fromisoformat(report["date"])
    lines: List[str] = [
        f"每日早报 · {format_date_cn(report_date)}",
        "=" * 46,
        "",
    ]

    if report.get("digest"):
        lines += [f"导读：{report['digest']}", ""]

    if report.get("degraded"):
        lines += [f"[注意] 本期为降级输出，原因：{report.get('degraded_reason', '未知')}", ""]

    for section in report.get("sections", []):
        items = section.get("items", [])
        lines.append(f"【{section.get('num', '')} {section['title']}】{len(items)} 条")
        if not items:
            lines.append("  （本时段无内容）")
        for idx, item in enumerate(items, start=1):
            lines.append(f"  {idx:02d}. {item['title']}")
            if item.get("summary"):
                lines.append(f"      {item['summary']}")
            if item.get("source"):
                lines.append(f"      来源：{item['source']}")
            lines.append(f"      原文：{item['url']}")
        lines.append("")

    lines += [
        "-" * 46,
        f"完整网页版（含往期存档）：{site_url}",
        f"本期共收录 {_total_items(report)} 条内容。",
        "正文摘要由 AI 生成，可能存在偏差；版权归原媒体所有，请以原文链接为准。",
    ]

    return "\n".join(lines)


# --------------------------------------------------------------------------
# HTML 版本（表格布局 + 行内样式，兼容主流邮箱客户端）
# --------------------------------------------------------------------------
def build_html(report: Dict[str, Any], site_url: str) -> str:
    report_date = date.fromisoformat(report["date"])
    date_cn = format_date_cn(report_date)

    rows: List[str] = []

    # ---- 报头 ----
    rows.append(
        f"""
        <tr><td style="padding:0 0 6px;border-bottom:2px solid {_INK};">
          <span style="font:700 12px/1.4 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                       letter-spacing:2px;text-transform:uppercase;color:{_INK};">
            每日早报 <span style="color:{_ACCENT};">/</span> Daily Digest
          </span>
        </td></tr>
        <tr><td style="padding:18px 0 4px;">
          <div style="font:700 30px/1.3 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                      color:{_ACCENT};letter-spacing:-0.5px;">{report_date.strftime('%d')}</div>
          <div style="font:400 12px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                      letter-spacing:1px;color:{_MUTED};">{report_date.strftime('%Y.%m')} · {date_cn}</div>
        </td></tr>
        """
    )

    # ---- 导读 ----
    if report.get("digest"):
        rows.append(
            f"""
            <tr><td style="padding:12px 0 16px;">
              <div style="border-left:3px solid {_ACCENT};padding-left:12px;
                          font:400 15px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                          color:{_INK_SOFT};">
                {_esc(report['digest'])}
              </div>
            </td></tr>
            """
        )

    # ---- 降级提示 ----
    if report.get("degraded"):
        rows.append(
            f"""
            <tr><td style="padding:0 0 16px;">
              <div style="border-left:3px solid {_ACCENT};padding:8px 12px;background:#fafafa;
                          font:400 13px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                          color:{_INK_SOFT};">
                <b>本期为降级输出</b>，AI 摘要环节未完成，以下为原始采集内容。
              </div>
            </td></tr>
            """
        )

    # ---- 板块 ----
    for section in report.get("sections", []):
        items = section.get("items", [])

        rows.append(
            f"""
            <tr><td style="padding:22px 0 0;">
              <div style="border-bottom:2px solid {_INK};padding-bottom:8px;">
                <span style="font:700 12px/1.4 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                             color:{_ACCENT};letter-spacing:1px;">{section.get('num', '')}</span>
                <span style="font:700 16px/1.4 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                             color:{_INK};margin-left:8px;">{_esc(section['title'])}</span>
                <span style="font:400 12px/1.4 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                             color:{_MUTED};margin-left:8px;">{len(items)} 条</span>
              </div>
            </td></tr>
            """
        )

        if not items:
            rows.append(
                f"""
                <tr><td style="padding:14px 0;border-bottom:1px solid {_RULE};
                               font:400 13px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                               color:{_MUTED};">
                  本时段该板块没有采集到内容。
                </td></tr>
                """
            )
            continue

        for idx, item in enumerate(items, start=1):
            meta_bits = []
            if item.get("source"):
                meta_bits.append(
                    f'<b style="color:{_INK};">{_esc(item["source"])}</b>'
                )
            if section.get("key") == "github" and item.get("stars"):
                lang = f' · {_esc(item["language"])}' if item.get("language") else ""
                meta_bits.append(f'{_esc(str(item["stars"]))} stars{lang}')

            meta_line = (
                f'<div style="font:400 11px/1.6 -apple-system,\'Segoe UI\',\'Microsoft YaHei\',sans-serif;'
                f'letter-spacing:0.6px;text-transform:uppercase;color:{_MUTED};padding-top:7px;">'
                f'{" &nbsp;·&nbsp; ".join(meta_bits)}</div>'
                if meta_bits
                else ""
            )

            summary_line = (
                f'<div style="font:400 14px/1.7 -apple-system,\'Segoe UI\',\'Microsoft YaHei\',sans-serif;'
                f'color:{_INK_SOFT};padding-top:5px;">{_esc(item.get("summary", ""))}</div>'
                if item.get("summary")
                else ""
            )

            # 原文标题：与中文标题不同的外文来源才展示，避免中文源重复
            original_line = ""
            if item.get("is_foreign") and item.get("title_original"):
                original_line = (
                    f'<div style="font:400 12px/1.6 -apple-system,\'Segoe UI\',\'Microsoft YaHei\',sans-serif;'
                    f'color:{_MUTED};padding-top:4px;">{_esc(item["title_original"])}</div>'
                )

            rows.append(
                f"""
                <tr><td style="padding:15px 0;border-bottom:1px solid {_RULE};">
                  <div style="font:700 15px/1.5 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                              color:{_INK};">
                    <span style="color:{_MUTED};font-weight:400;font-size:12px;">{idx:02d}</span>&nbsp;
                    <a href="{_esc(item['url'])}" style="color:{_INK};text-decoration:none;">{_esc(item['title'])}</a>
                  </div>
                  {original_line}
                  {summary_line}
                  <div style="padding-top:8px;">
                    <a href="{_esc(item['url'])}"
                       style="font:700 11px/1.4 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                              letter-spacing:0.8px;text-transform:uppercase;color:{_ACCENT};
                              text-decoration:none;border-bottom:1px solid {_ACCENT};padding-bottom:1px;">
                      阅读全文 &rsaquo;
                    </a>
                    {meta_line}
                  </div>
                </td></tr>
                """
            )

    # ---- 页脚 ----
    total = _total_items(report)
    rows.append(
        f"""
        <tr><td style="padding:26px 0 0;border-top:2px solid {_INK};">
          <div style="font:400 12px/1.9 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:{_MUTED};">
            本期共收录 <b style="color:{_INK_SOFT};">{total}</b> 条内容 ·
            原始素材 {report.get('_raw_news', '—')} 条新闻。<br>
            正文摘要由 AI 生成，可能存在偏差；版权归原媒体所有，请以「阅读全文」指向的原文为准。<br>
            由 GitHub Actions 每日自动生成 · 摘要由 DeepSeek 产出。
          </div>
          <div style="padding-top:14px;">
            <a href="{_esc(site_url)}"
               style="font:700 12px/1.4 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
                      letter-spacing:0.8px;color:{_PAPER};background:{_INK};
                      text-decoration:none;padding:10px 18px;display:inline-block;">
              查看网页版与往期存档 &rsaquo;
            </a>
          </div>
        </td></tr>
        """
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(build_subject(report))}</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f5;">
  <!-- 外层表格负责背景色，内层定宽 640px：这是邮件客户端的通用兼容写法 -->
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background:#f4f4f5;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:640px;background:{_PAPER};padding:28px 26px;">
          {''.join(rows)}
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# --------------------------------------------------------------------------
# 发送
# --------------------------------------------------------------------------
def _smtp_connect() -> smtplib.SMTP:
    """建立 SMTP 连接并登录。465 用 SSL，其余走 STARTTLS。"""
    if config.SMTP_USE_SSL:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT
        )
    else:
        server = smtplib.SMTP(
            config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT
        )
        server.ehlo()
        server.starttls()
        server.ehlo()

    server.login(config.SMTP_USER, config.SMTP_PASS)
    return server


def send_email(
    report: Dict[str, Any],
    site_url: Optional[str] = None,
    dry_run: bool = False,
) -> bool:
    """
    发送早报邮件。返回是否成功（dry_run 时返回 True）。

    所有异常都在这里被捕获，不会向上抛 —— 页面已经生成好了，
    邮件失败不应该让整个 Actions 任务变成红色。
    """
    site_url = site_url or config.resolve_site_url()

    if dry_run:
        logger.info("[dry-run] 跳过邮件发送")
        return True

    if not config.NOTIFY_ENABLED:
        logger.info("NOTIFY_ENABLED=false，跳过邮件发送")
        return True

    missing = [
        name
        for name, value in (
            ("SMTP_USER", config.SMTP_USER),
            ("SMTP_PASS", config.SMTP_PASS),
            ("RECEIVER_EMAIL", config.RECEIVER_EMAIL),
        )
        if not value
    ]
    if missing:
        logger.warning("邮件配置不完整，缺少 %s，跳过发送", "、".join(missing))
        return False

    recipients = config.resolve_recipients()
    if not recipients:
        logger.warning("没有可用的收件人，跳过发送")
        return False

    # 统计信息挂到 report 上供 HTML 页脚使用
    report.setdefault("_raw_news", "—")

    subject = build_subject(report)
    plain = build_plain_text(report, site_url)
    html = build_html(report, site_url)

    message = MIMEMultipart("alternative")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = formataddr((str(Header(config.SMTP_FROM_NAME, "utf-8")), config.SMTP_USER))
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=config.SMTP_USER.split("@")[-1] or "localhost")

    # 顺序不能反：最后一个 part 是客户端优先渲染的版本
    message.attach(MIMEText(plain, "plain", "utf-8"))
    message.attach(MIMEText(html, "html", "utf-8"))

    logger.info(
        "发送早报邮件：%s → %s（正文 %.1f KB）",
        config.SMTP_HOST,
        ", ".join(recipients),
        len(html.encode("utf-8")) / 1024,
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        server = None
        try:
            server = _smtp_connect()
            refused = server.sendmail(config.SMTP_USER, recipients, message.as_string())
            if refused:
                # 部分收件人被拒不算致命，但仍要记下来
                logger.warning("以下收件人被服务器拒绝：%s", refused)
            logger.info("邮件发送成功（收件人 %d 个）", len(recipients) - len(refused))
            return True

        except smtplib.SMTPAuthenticationError as exc:
            # 认证失败重试没有意义，通常是授权码错误或未开启 SMTP 服务
            logger.error(
                "SMTP 认证失败：%s。请确认 SMTP_PASS 填的是邮箱「授权码」而非登录密码，"
                "且已在邮箱设置里开启 SMTP 服务。",
                exc,
            )
            return False

        except (smtplib.SMTPException, OSError) as exc:
            last_error = exc
            wait = min(2 ** attempt, 15)
            logger.warning("邮件发送失败（第 %d/3 次）：%s", attempt, exc)
            if attempt < 3:
                time.sleep(wait)

        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001 —— 关闭连接的异常无关紧要
                    pass

    logger.error("邮件发送在 3 次尝试后仍失败：%s", last_error)
    return False


__all__ = ["send_email", "build_subject", "build_plain_text", "build_html"]
