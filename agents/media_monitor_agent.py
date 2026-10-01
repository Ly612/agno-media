"""Media Monitor Agent：国际媒体监控助手（工具型 Agent）。

界面上显示为一个 Agent。内部把 `utils.media_monitor_pipeline.MediaMonitorPipeline`
的五阶段流水线封装成若干「工具函数」供 LLM 调用：

    - run_media_monitor:  一键跑完整流水线（抓首页→挑话题→详情总结→社交板块→渲染→Word→推送）
    - scrape_homepages:   只跑 Phase 1（抓取 23 家媒体首页）
    - select_topics:      只跑 Phase 2（LLM 挑选话题）
    - summarize_topics:   只跑 Phase 3（LLM 详情总结）
    - render_report:      只跑 Phase 5（渲染简报 + Word）

用户在聊天框说「运行今天的媒体监控」即触发 run_media_monitor。所有工具返回
JSON 字符串（含 report_file / word_file 等关键字段），便于 LLM 展示。

各外部服务（BrightData / Apify / 飞书 / 邮件 / 截图）无凭证时对应阶段自动跳过，
不阻塞核心流程；只要配置 BrightData key 即可跑通核心三阶段。
"""

from __future__ import annotations

import json
from typing import Any, Dict

from agno.agent import Agent

from utils.config import get_agent_config
from utils.db import get_db
from utils.model import get_model
from utils.media_monitor_pipeline import MediaMonitorPipeline
from utils.settings import (
    MM_PUSH_EMAIL,
    MM_PUSH_FEISHU,
    MM_SKIP_SCREENSHOT,
    MM_SKIP_SOCIAL,
    MM_SKIP_WORD,
)


def _dumps(obj: Dict[str, Any]) -> str:
    """把结果 dict 序列化成 JSON 字符串（供 LLM 读取展示）。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════════
# 工具函数（注册到 agno Agent，由 LLM 调用）
# ═══════════════════════════════════════════════════════════


def run_media_monitor(
    skip_social: bool = MM_SKIP_SOCIAL,
    skip_screenshot: bool = MM_SKIP_SCREENSHOT,
    skip_word: bool = MM_SKIP_WORD,
    push_feishu: bool = MM_PUSH_FEISHU,
    push_email: bool = MM_PUSH_EMAIL,
) -> str:
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
        JSON 字符串，含 report_file / word_file / topics_file / summaries_file /
        feishu_pushed / email_sent 等字段；失败时含 error 字段。
    """
    pipeline = MediaMonitorPipeline()
    result = pipeline.run_full_pipeline(
        skip_social=skip_social,
        skip_screenshot=skip_screenshot,
        skip_word=skip_word,
        push_feishu=push_feishu,
        push_email=push_email,
    )
    # report 正文可能很长，返回给 LLM 时截断，避免撑爆上下文
    if "report" in result and isinstance(result["report"], str) and len(result["report"]) > 2000:
        result = dict(result)
        result["report_preview"] = result.pop("report")[:2000] + " ...(已截断)"
    return _dumps(result)


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


def render_report() -> str:
    """只运行 Phase 5：把已有摘要渲染成 Markdown 简报并转 Word。

    需先运行过 summarize_topics（存在 summaries.json）。

    Returns:
        JSON 字符串，含 report_file / word_file；失败含 error。
    """
    pipeline = MediaMonitorPipeline()
    report = pipeline.render_report()
    if "error" in report:
        return _dumps(report)
    word_file = ""
    if report.get("output_file"):
        word_file = pipeline.convert_report_to_word(report["output_file"]) or ""
    return _dumps(
        {
            "report_file": report.get("output_file", ""),
            "word_file": word_file,
        }
    )


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
                "整条流水线耗时较长（抓取 + 多次 LLM 调用），触发前可先简要告知用户正在执行。",
            ],
        ),
        add_history_to_context=True,
        num_history_runs=cfg.get("num_history_runs", 3),
        markdown=True,
    )
