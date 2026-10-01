"""进程内定时任务：按 config.yaml 的 scheduler 段自动跑媒体监控流水线。

用 APScheduler 的 BackgroundScheduler，随 main.py 启动。到点调用
`MediaMonitorPipeline.run_full_pipeline(...)`，推送开关沿用 config.yaml 的
media_monitor 段（push_feishu / push_email）。job 内 try/except 兜底，失败时
可选发飞书文本报警，不影响服务进程。

调度时间：优先用 `scheduler.cron`（五段式 "m h dom mon dow"），留空则退回
`hour`/`minute`/`day_of_week`。无 BrightData key 时打印警告、不注册 job，
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
    MM_PUSH_EMAIL,
    MM_PUSH_FEISHU,
    MM_SKIP_SCREENSHOT,
    MM_SKIP_SOCIAL,
    MM_SKIP_WORD,
    SCHEDULER_CRON,
    SCHEDULER_DAY_OF_WEEK,
    SCHEDULER_ENABLED,
    SCHEDULER_HOUR,
    SCHEDULER_MINUTE,
    SCHEDULER_TIMEZONE,
)

# 全局持有，避免被 GC；也防止 reload 下重复启动
_scheduler = None


def _run_media_monitor_job() -> None:
    """定时 job 实际执行体：跑完整流水线，异常兜底发飞书文本报警。"""
    # 延迟导入：pipeline 依赖较重，仅在真正触发时加载
    from utils.media_monitor_pipeline import MediaMonitorPipeline

    print("⏰ [scheduler] 触发媒体监控流水线 ...")
    try:
        pipeline = MediaMonitorPipeline()
        result = pipeline.run_full_pipeline(
            skip_social=MM_SKIP_SOCIAL,
            skip_screenshot=MM_SKIP_SCREENSHOT,
            skip_word=MM_SKIP_WORD,
            push_feishu=MM_PUSH_FEISHU,
            push_email=MM_PUSH_EMAIL,
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
        # best-effort 飞书报警（无凭证时 send_text 自行返回错误、不抛出）
        try:
            from utils.feishu_bot import FeishuBot

            FeishuBot().send_text(f"媒体监控定时任务执行失败: {e}")
        except Exception:
            pass


def _build_trigger_kwargs() -> dict:
    """根据配置构造 CronTrigger 的参数。cron 优先，否则用 hour/minute/day_of_week。"""
    if SCHEDULER_CRON:
        parts = SCHEDULER_CRON.split()
        if len(parts) != 5:
            raise ValueError(
                f"scheduler.cron 必须是五段式 'm h dom mon dow'，当前: {SCHEDULER_CRON!r}"
            )
        minute, hour, dom, month, dow = parts
        return {
            "minute": minute,
            "hour": hour,
            "day": dom,
            "month": month,
            "day_of_week": dow,
        }
    return {
        "hour": SCHEDULER_HOUR,
        "minute": SCHEDULER_MINUTE,
        "day_of_week": SCHEDULER_DAY_OF_WEEK,
    }


def start_scheduler():
    """按配置启动进程内定时任务，返回 scheduler 实例（未启动时返回 None）。

    - scheduler.enabled=false：不启动，直接返回 None。
    - 无 BrightData key：打印警告、不注册 job（流水线无法跑核心阶段）。
    - 已启动过（reload 重复调用）：直接返回已有实例，不重复注册。
    """
    global _scheduler

    if not SCHEDULER_ENABLED:
        print("ℹ [scheduler] 定时任务未启用（config.yaml scheduler.enabled=false）")
        return None

    if _scheduler is not None:
        print("ℹ [scheduler] 调度器已在运行，跳过重复启动")
        return _scheduler

    if not BRIGHTDATA_API_KEY:
        print(
            "⚠ [scheduler] 未配置 BRIGHT_DATA_API_KEY，媒体监控无法抓取，"
            "跳过注册定时任务"
        )
        return None

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

    try:
        trigger_kwargs = _build_trigger_kwargs()
    except ValueError as e:
        print(f"⚠ [scheduler] {e}，跳过注册定时任务")
        return None

    scheduler = BackgroundScheduler(timezone=SCHEDULER_TIMEZONE)
    trigger = CronTrigger(timezone=SCHEDULER_TIMEZONE, **trigger_kwargs)
    scheduler.add_job(
        _run_media_monitor_job,
        trigger=trigger,
        id="media_monitor_daily",
        name="媒体监控日报",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler

    schedule_desc = SCHEDULER_CRON or (
        f"{SCHEDULER_HOUR:02d}:{SCHEDULER_MINUTE:02d} "
        f"(day_of_week={SCHEDULER_DAY_OF_WEEK})"
    )
    print(
        f"✅ [scheduler] 媒体监控定时任务已启动: {schedule_desc} "
        f"[{SCHEDULER_TIMEZONE}]"
    )
    return scheduler


def shutdown_scheduler() -> None:
    """停止调度器（可选，进程退出时调用）。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
