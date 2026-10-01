"""Media Monitor Agent：国际媒体监控助手（工具型 Agent）。

界面上显示为一个 Agent。内部把 `utils.media_monitor_pipeline.MediaMonitorPipeline`
的五阶段流水线封装成若干「工具函数」供 LLM 调用：

    - run_media_monitor:  一键跑完整流水线（抓首页→挑话题→详情总结→社交板块→渲染→Word→推送）
    - scrape_homepages:   只跑 Phase 1（抓取 23 家媒体首页）
    - select_topics:      只跑 Phase 2（LLM 挑选话题）
    - summarize_topics:   只跑 Phase 3（LLM 详情总结）
    - render_report:      只跑 Phase 5（渲染简报 + Word）

用户在聊天框说「运行今天的媒体监控」即触发 run_media_monitor。

返回值约定：
- 会生成 Word 日报的工具（run_media_monitor / render_report）返回 agno 的
  `ToolResult`，把 .docx 作为 `File` 附件挂在工具结果上——agno 会把它透传到
  本次 run 的响应里，前端 AgentUI 直接渲染成可下载的文件附件（无需额外下载链接）。
- 其余分步工具返回 JSON 字符串（供 LLM 展示关键字段）。

各外部服务（BrightData / Apify / 飞书 / 邮件 / 截图）无凭证时对应阶段自动跳过，
不阻塞核心流程；只要配置 BrightData key 即可跑通核心三阶段。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from agno.agent import Agent
from agno.media import File
from agno.tools.function import ToolResult

from utils.config import get_agent_config
from utils.db import get_db
from utils.model import get_model
from utils.media_monitor_pipeline import MediaMonitorPipeline
from utils.settings import (
    DOWNLOAD_BASE_URL,
    EXPORT_DIR,
    MM_PUSH_EMAIL,
    MM_PUSH_FEISHU,
    MM_SKIP_SCREENSHOT,
    MM_SKIP_SOCIAL,
    MM_SKIP_WORD,
)

# .docx 的标准 MIME
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _dumps(obj: Dict[str, Any]) -> str:
    """把结果 dict 序列化成 JSON 字符串（供 LLM 读取展示）。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _publish_download(word_file: str) -> str:
    """把 Word 日报复制到 exports/ 目录，返回可点击下载的对外 URL。

    开源 AgentUI 目前不渲染 agno 原生文件附件，故同时提供 /download 下载链接兜底
    （与 News Agent 的 Excel 下载同一机制）。路径无效/文件不存在时返回空串。
    """
    if not word_file:
        return ""
    src = Path(word_file)
    if not src.is_file():
        return ""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    dst = EXPORT_DIR / src.name
    # 已存在同名（同一报告重复导出）时覆盖即可
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    return f"{DOWNLOAD_BASE_URL}/{src.name}"


def _word_file_artifact(word_file: str) -> Optional[File]:
    """把磁盘上的 .docx 读成 agno File 附件；路径无效/文件不存在时返回 None。"""
    if not word_file:
        return None
    path = Path(word_file)
    if not path.is_file():
        return None
    data = path.read_bytes()
    return File(
        id=str(uuid4()),
        content=data,
        filepath=str(path),
        filename=path.name,
        mime_type=_DOCX_MIME,
        file_type="docx",
        size=len(data),
    )


def _result_with_word(summary: Dict[str, Any], word_file: str) -> ToolResult:
    """构造带 Word 附件 + 下载链接的 ToolResult。

    - files：挂 agno 原生文件附件（支持的前端会直接渲染下载）。
    - content.download_url：/download 兜底链接（开源 AgentUI 用它点击下载）。

    Word 文件不存在（未转 Word / 无 pandoc）时只返回文本，不挂附件、不给链接。
    """
    summary = dict(summary)
    download_url = _publish_download(word_file)
    if download_url:
        summary["download_url"] = download_url

    artifact = _word_file_artifact(word_file)
    content = _dumps(summary)
    if artifact is None:
        return ToolResult(content=content)
    return ToolResult(content=content, files=[artifact])


# ═══════════════════════════════════════════════════════════
# 工具函数（注册到 agno Agent，由 LLM 调用）
# ═══════════════════════════════════════════════════════════


def run_media_monitor(
    skip_social: bool = MM_SKIP_SOCIAL,
    skip_screenshot: bool = MM_SKIP_SCREENSHOT,
    skip_word: bool = MM_SKIP_WORD,
    push_feishu: bool = MM_PUSH_FEISHU,
    push_email: bool = MM_PUSH_EMAIL,
) -> ToolResult:
    """运行完整的媒体监控五阶段流水线，生成当日日报。

    抓取 23 家国际主流媒体首页 → LLM 挑选全球与涉华话题 → 逐话题详情总结
    → 海外社交平台板块 → 渲染 Markdown 简报 → 转 Word → 可选推送飞书/邮件。

    Args:
        skip_social:     跳过社交平台板块（未配置 APIFY_API_KEY 时无论如何都会跳过）
        skip_screenshot: 跳过信源网站首页截图板块（无 Playwright 时自动跳过）
        skip_word:       跳过 Word 转换（无 pandoc 时自动跳过）
        push_feishu:     生成后把 Word 日报推送到飞书群（需 FEISHU_* 凭证）
        push_email:      生成后把 Word 日报作为附件发邮件（需 MEDIA_MONITOR_SMTP_* 凭证）

    Returns:
        ToolResult：content 为 JSON（含 report_file / word_file / topics_file /
        summaries_file / feishu_pushed / email_sent 等，失败时含 error），
        若成功生成 Word 则把 .docx 作为文件附件挂在 files 上，供前端下载。
    """
    pipeline = MediaMonitorPipeline()
    result = pipeline.run_full_pipeline(
        skip_social=skip_social,
        skip_screenshot=skip_screenshot,
        skip_word=skip_word,
        push_feishu=push_feishu,
        push_email=push_email,
    )
    # 流水线出错：没有产物可挂，直接把错误 JSON 返回给 LLM
    if "error" in result:
        return ToolResult(content=_dumps(result))

    # report 正文可能很长，返回给 LLM 时截断，避免撑爆上下文
    summary = dict(result)
    report = summary.pop("report", "")
    if isinstance(report, str) and report:
        summary["report_preview"] = (report[:2000] + " ...(已截断)") if len(report) > 2000 else report

    return _result_with_word(summary, result.get("word_file", ""))


def scrape_homepages() -> str:
    """只运行 Phase 1：抓取 23 家国际主流媒体首页首屏内容。

    Returns:
        JSON 字符串，含抓取统计与 scrape_manifest.json 路径；失败含 error。
    """
    pipeline = MediaMonitorPipeline()
    return _dumps(pipeline.scrape_all_homepages())


def select_topics() -> str:
    """只运行 Phase 2：用 LLM 从已抓取的首页内容中挑选重要话题。

    需先运行过 scrape_homepages（存在 scrape_manifest.json）。

    Returns:
        JSON 字符串，含 global_topics / china_topics / topics.json 路径；失败含 error。
    """
    pipeline = MediaMonitorPipeline()
    return _dumps(pipeline.select_topics())


def summarize_topics() -> str:
    """只运行 Phase 3：用 LLM 对已挑选话题逐条抓正文并生成多方观点摘要。

    需先运行过 select_topics（存在 topics.json）。

    Returns:
        JSON 字符串，含摘要统计与 summaries.json 路径；失败含 error。
    """
    pipeline = MediaMonitorPipeline()
    return _dumps(pipeline.summarize_all_topics())


def render_report() -> ToolResult:
    """只运行 Phase 5：把已有摘要渲染成 Markdown 简报并转 Word。

    需先运行过 summarize_topics（存在 summaries.json）。

    Returns:
        ToolResult：content 为 JSON（含 report_file / word_file；失败含 error），
        成功生成 Word 时把 .docx 作为文件附件挂在 files 上，供前端下载。
    """
    pipeline = MediaMonitorPipeline()
    report = pipeline.render_report()
    if "error" in report:
        return ToolResult(content=_dumps(report))
    word_file = ""
    if report.get("output_file"):
        word_file = pipeline.convert_report_to_word(report["output_file"]) or ""
    summary = {
        "report_file": report.get("output_file", ""),
        "word_file": word_file,
    }
    return _result_with_word(summary, word_file)


# ═══════════════════════════════════════════════════════════
# Agent 构建（供 main.py 自动发现）
# ═══════════════════════════════════════════════════════════


def build_agent() -> Agent:
    cfg = get_agent_config("media_monitor_agent")
    return Agent(
        name=cfg.get("name", "Media Monitor Agent"),
        model=get_model(),
        db=get_db(),
        tools=[
            run_media_monitor,
            scrape_homepages,
            select_topics,
            summarize_topics,
            render_report,
        ],
        instructions=cfg.get(
            "instructions",
            [
                "你是国际媒体监控助手，能抓取 23 家国际主流媒体首页、挑选全球与"
                "涉华话题、生成多方观点摘要并渲染日报。",
                "用户要求「运行媒体监控 / 跑今天的日报」时，调用 run_media_monitor 工具；"
                "需要分步执行时调用对应阶段工具（scrape_homepages / select_topics / "
                "summarize_topics / render_report）。",
                "工具返回 JSON（含 report_file / word_file 等），请用中文简要说明结果，"
                "并把报告文件路径清晰列出。",
                "当工具返回的 JSON 里含 download_url 字段时，务必在回复末尾把它渲染成"
                "可点击的 markdown 链接，例如 [下载 Word 日报](download_url)；这是用户下载"
                ".docx 的主要方式。不要编造或改写该链接。",
                "整条流水线耗时较长（抓取 + 多次 LLM 调用），触发前可先简要告知用户正在执行。",
            ],
        ),
        add_history_to_context=True,
        num_history_runs=cfg.get("num_history_runs", 3),
        markdown=True,
    )
