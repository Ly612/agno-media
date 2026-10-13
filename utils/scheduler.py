"""进程内定时任务：按 config.yaml 的 scheduler 段自动跑媒体监控流水线。

用 APScheduler 的 BackgroundScheduler，随 main.py 启动。到点调用
`DailyReportPipeline.run_full_pipeline(...)`，仅生成 Word 日报缓存到
output/daily_report，不做任何推送。job 内 try/except 兜底，异常只打印日志、
不影响服务进程。生成的最新 Word 由 MCP 工具（mcp_server.py 的
latest_daily_report）按需读取返回。

调度时间：优先用 `scheduler.cron`（五段式 "m h dom mon dow"），留空则退回
`hour`/`minute`/`day_of_week`。默认配置为每天 13:00 和 16:00
（cron: "0 13,16 * * *"）。无 BrightData key 时打印警告、不注册 job，
不阻塞服务启动。

用法（在 main.py 的 __main__ 块里）:
    from utils.scheduler import start_scheduler
    start_scheduler()
"""

from __future__ import annotations

import traceback
from typing import Optional

from utils.settings import (
    BRIGHTDATA_API_KEY,
    CLEANUP_CRON,
    CLEANUP_ENABLED,
    DR_SKIP_SCREENSHOT,
    DR_SKIP_SOCIAL,
    DR_SKIP_WORD,
    SCHEDULER_CRON,
    SCHEDULER_DAY_OF_WEEK,
    SCHEDULER_ENABLED,
    SCHEDULER_HOUR,
    SCHEDULER_MINUTE,
    SCHEDULER_TIMEZONE,
)

# 全局持有，避免被 GC；也防止 reload 下重复启动
_scheduler = None


def _run_daily_report_job() -> None:
    """定时 job 实际执行体：跑完整流水线，仅生成 Word 缓存，不做任何推送。"""
    # 延迟导入：pipeline 依赖较重，仅在真正触发时加载
    from utils.daily_report_pipeline import DailyReportPipeline

    print("⏰ [scheduler] 触发媒体监控流水线 ...")
    try:
        pipeline = DailyReportPipeline()
        result = pipeline.run_full_pipeline(
            skip_social=DR_SKIP_SOCIAL,
            skip_screenshot=DR_SKIP_SCREENSHOT,
            skip_word=DR_SKIP_WORD,
        )
        if "error" in result:
            print(f"⚠ [scheduler] 流水线返回错误: {result['error']}")
        else:
            print(
                f"✅ [scheduler] 流水线完成: {result.get('report_file', '')} "
                f"| Word: {result.get('word_file') or '(无)'}"
            )
    except Exception as e:  # noqa: BLE001 - 定时 job 必须吞异常，否则会杀掉调度线程
        print(f"❌ [scheduler] 流水线异常: {e}")
        traceback.print_exc()


def _parse_cron(cron: str, field_name: str) -> dict:
    """把五段式 cron 字符串解析成 CronTrigger 的 kwargs。"""
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(
            f"{field_name} 必须是五段式 'm h dom mon dow'，当前: {cron!r}"
        )
    minute, hour, dom, month, dow = parts
    return {
        "minute": minute,
        "hour": hour,
        "day": dom,
        "month": month,
        "day_of_week": dow,
    }


def _build_trigger_kwargs() -> dict:
    """根据配置构造日报 CronTrigger 的参数。cron 优先，否则用 hour/minute/day_of_week。"""
    if SCHEDULER_CRON:
        return _parse_cron(SCHEDULER_CRON, "scheduler.cron")
    return {
        "hour": SCHEDULER_HOUR,
        "minute": SCHEDULER_MINUTE,
        "day_of_week": SCHEDULER_DAY_OF_WEEK,
    }


def _register_daily_report_job(scheduler, CronTrigger) -> bool:
    """注册媒体监控日报 job。无 BrightData key 或 cron 非法时跳过，返回是否注册成功。"""
    if not BRIGHTDATA_API_KEY:
        print(
            "⚠ [scheduler] 未配置 BRIGHT_DATA_API_KEY，媒体监控无法抓取，"
            "跳过注册日报定时任务"
        )
        return False
    try:
        trigger_kwargs = _build_trigger_kwargs()
    except ValueError as e:
        print(f"⚠ [scheduler] {e}，跳过注册日报定时任务")
        return False

    scheduler.add_job(
        _run_daily_report_job,
        trigger=CronTrigger(timezone=SCHEDULER_TIMEZONE, **trigger_kwargs),
        id="daily_report_daily",
        name="媒体监控日报",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    schedule_desc = SCHEDULER_CRON or (
        f"{SCHEDULER_HOUR:02d}:{SCHEDULER_MINUTE:02d} "
        f"(day_of_week={SCHEDULER_DAY_OF_WEEK})"
    )
    print(f"✅ [scheduler] 媒体监控日报已注册: {schedule_desc} [{SCHEDULER_TIMEZONE}]")
    return True


def _register_cleanup_job(scheduler, CronTrigger) -> bool:
    """注册临时文件清理 job。未启用或 cron 非法时跳过，返回是否注册成功。"""
    if not CLEANUP_ENABLED:
        print("ℹ [scheduler] 临时文件清理未启用（scheduler.cleanup.enabled=false）")
        return False
    try:
        trigger_kwargs = _parse_cron(CLEANUP_CRON, "scheduler.cleanup.cron")
    except ValueError as e:
        print(f"⚠ [scheduler] {e}，跳过注册清理定时任务")
        return False

    # 延迟导入：仅在启用清理时才引入
    from utils.cleanup import run_cleanup_job

    scheduler.add_job(
        run_cleanup_job,
        trigger=CronTrigger(timezone=SCHEDULER_TIMEZONE, **trigger_kwargs),
        id="temp_files_cleanup",
        name="临时文件清理",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    print(f"✅ [scheduler] 临时文件清理已注册: {CLEANUP_CRON} [{SCHEDULER_TIMEZONE}]")
    return True


def start_scheduler():
    """按配置启动进程内定时任务，返回 scheduler 实例（未启动时返回 None）。

    - scheduler.enabled=false：不启动，直接返回 None。
    - 日报 job 与清理 job 相互独立：无 BrightData key 时仍会注册清理 job。
    - 两个 job 都没注册成功时，不启动空调度器，返回 None。
    - 已启动过（reload 重复调用）：直接返回已有实例，不重复注册。
    """
    global _scheduler

    if not SCHEDULER_ENABLED:
        print("ℹ [scheduler] 定时任务未启用（config.yaml scheduler.enabled=false）")
        return None

    if _scheduler is not None:
        print("ℹ [scheduler] 调度器已在运行，跳过重复启动")
        return _scheduler

    # 延迟导入 APScheduler：只有真正启用定时任务时才需要这个依赖
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        print(
            "⚠ [scheduler] 未安装 apscheduler，无法启动定时任务。"
            "请先 `pip install apscheduler`"
        )
        return None

    scheduler = BackgroundScheduler(timezone=SCHEDULER_TIMEZONE)
    registered_report = _register_daily_report_job(scheduler, CronTrigger)
    registered_cleanup = _register_cleanup_job(scheduler, CronTrigger)

    if not (registered_report or registered_cleanup):
        print("⚠ [scheduler] 无任何定时任务注册成功，不启动调度器")
        return None

    scheduler.start()
    _scheduler = scheduler
    return scheduler


def shutdown_scheduler() -> None:
    """停止调度器（可选，进程退出时调用）。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
