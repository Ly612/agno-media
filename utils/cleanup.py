"""临时文件清理：删除超过保留时长的旧文件。

由定时任务（utils/scheduler.py）每天夜里调用一次。清理两类目录：

1. exports/ —— News Agent 的 Excel 导出、Daily Report Agent 复制过来的 .docx
   日报（用户交互时产生的对外下载副本）。递归清理整个目录下超时的文件。

2. output/daily_report/ —— 流水线工作目录。**递归清理全部超时文件**，包括最终产物
   report_*.md / report_*.docx，以及 topics.json / summaries.json 等中间文件、
   raw/ / screenshots/ / social/ 子目录内的所有内容。

两类目录统一按**最后修改时间（mtime）**判断：早于保留窗口的文件全部删除。
删空后留下的空子目录会一并清掉（但保留两个顶层目录本身）。

安全约束：
- 只删除**普通文件**（及清理后变空的子目录），不动顶层目标目录本身。
- 删除前校验文件确实位于目标目录内（解析真实路径，防越界）。
- 单个文件删除失败只打印警告、继续处理其余文件，不抛出中断。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, Tuple

from utils.settings import (
    CLEANUP_MAX_AGE_HOURS,
    DAILY_REPORT_OUTPUT_DIR,
    EXPORT_DIR,
)

# 清理时忽略的文件名（系统/隐藏文件，避免噪音）
_IGNORE_NAMES = {".DS_Store", ".gitkeep", ".gitignore"}


def _is_inside(path: Path, base: Path) -> bool:
    """校验 path 的真实路径确实位于 base 目录内（防路径越界误删）。"""
    try:
        path_resolved = path.resolve()
        base_resolved = base.resolve()
    except OSError:
        return False
    return base_resolved == path_resolved.parent or base_resolved in path_resolved.parents


def _iter_all_files(root: Path) -> Iterable[Path]:
    """递归列出 root 下的所有普通文件（含子目录），跳过忽略名单。"""
    if not root.exists():
        return []
    return [
        p
        for p in root.rglob("*")
        if p.is_file() and p.name not in _IGNORE_NAMES
    ]


def _remove_empty_subdirs(root: Path) -> None:
    """清理 root 下因删文件而变空的子目录（自底向上，保留 root 本身）。"""
    if not root.exists():
        return
    # 收集所有子目录，按路径深度降序，先删最深层
    subdirs = sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for d in subdirs:
        try:
            if _is_inside(d, root) and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass  # 非空或无权限，跳过


def _cleanup_dir(
    label: str,
    base: Path,
    files: Iterable[Path],
    cutoff_ts: float,
) -> Tuple[int, int]:
    """删除 files 中早于 cutoff_ts 的文件。返回 (已删数量, 失败数量)。"""
    removed = 0
    failed = 0
    for f in files:
        try:
            if not f.is_file():
                continue
            if not _is_inside(f, base):
                print(f"⚠ [cleanup] 跳过越界文件: {f}")
                continue
            if f.stat().st_mtime >= cutoff_ts:
                continue  # 还在保留窗口内
            try:
                rel = f.relative_to(base)
            except ValueError:
                rel = f.name
            f.unlink()
            removed += 1
            print(f"   🗑 [cleanup] 删除 {label}: {rel}")
        except OSError as e:
            failed += 1
            print(f"⚠ [cleanup] 删除失败 {f}: {e}")
    return removed, failed


def cleanup_temp_files(max_age_hours: float | None = None) -> dict:
    """清理 exports/ 与 output/daily_report/ 下超过保留时长的旧文件（含中间文件）。

    两个目录都**递归**清理：早于保留窗口的普通文件全部删除，删完后清掉变空的子目录
    （保留两个顶层目录本身）。

    Args:
        max_age_hours: 保留时长（小时），早于此的文件被删除。
                       默认取 settings.CLEANUP_MAX_AGE_HOURS（config.yaml 配置）。

    Returns:
        {"removed": 总删除数, "failed": 失败数, "max_age_hours": 实际用的阈值}
    """
    age = CLEANUP_MAX_AGE_HOURS if max_age_hours is None else float(max_age_hours)
    cutoff_ts = time.time() - age * 3600

    total_removed = 0
    total_failed = 0

    for label, base in (
        ("exports", EXPORT_DIR),
        ("daily_report", DAILY_REPORT_OUTPUT_DIR),
    ):
        r, f = _cleanup_dir(label, base, _iter_all_files(base), cutoff_ts)
        total_removed += r
        total_failed += f
        _remove_empty_subdirs(base)

    return {"removed": total_removed, "failed": total_failed, "max_age_hours": age}


def run_cleanup_job() -> None:
    """定时 job 执行体：清理临时文件，吞异常、只打印日志，不影响调度线程。"""
    print(f"🧹 [cleanup] 开始清理临时文件（保留 {CLEANUP_MAX_AGE_HOURS:g} 小时内）...")
    try:
        result = cleanup_temp_files()
        print(
            f"✅ [cleanup] 完成：删除 {result['removed']} 个文件"
            f"（失败 {result['failed']} 个）"
        )
    except Exception as e:  # noqa: BLE001 - 定时 job 必须吞异常
        import traceback

        print(f"❌ [cleanup] 清理异常: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    """命令行手动清理：python -m utils.cleanup [max_age_hours]"""
    import sys

    arg_age = float(sys.argv[1]) if len(sys.argv) > 1 else None
    out = cleanup_temp_files(arg_age)
    print(
        f"清理完成：删除 {out['removed']} 个文件，失败 {out['failed']} 个"
        f"（阈值 {out['max_age_hours']:g} 小时）"
    )
