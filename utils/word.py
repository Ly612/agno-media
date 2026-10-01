"""Markdown → Word(docx) 转换工具。

基于 pypandoc 将日报 Markdown 转换为 Word 文档，保留标题层级、列表、
链接、粗体等格式。依赖系统已安装的 pandoc 二进制（macOS/Linux 可用
`brew install pandoc`；转换时会自动从 PATH 找到）。

用法:
    python -m tools.word                              # 转换最新一份报告 report_*.md
    python -m tools.word <input.md>                   # 指定输入 Markdown
    python -m tools.word <input.md> <output.docx>     # 指定输入与输出

示例:
    python -m tools.word output/media_monitor/report_20260810_150626.md

输出 docx 默认与输入同目录、同名。若同目录存在 reference.docx，
则作为样式参考模板（--reference-doc）；否则使用 pandoc 默认样式。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Optional

import pypandoc

DEFAULT_OUTPUT_DIR = Path(__file__).parent.parent / "output" / "media_monitor"


def _find_latest_report() -> Optional[Path]:
    """在默认输出目录找最新的一份 report_*.md。"""
    if not DEFAULT_OUTPUT_DIR.exists():
        return None
    reports = sorted(DEFAULT_OUTPUT_DIR.glob("report_*.md"))
    return reports[-1] if reports else None


def _find_reference_doc(target_dir: Path) -> Optional[Path]:
    """同目录下若有 reference.docx 则用作样式参考模板。"""
    candidate = target_dir / "reference.docx"
    return candidate if candidate.exists() else None


def _report_date_line(report_path: Path) -> Optional[str]:
    """从报告文件名 report_YYYYMMDD_HHMMSS.md 提取日期，返回 markdown 日期行。

    例如 report_20260811_104724.md → "**报告日期：2026年8月11日**"。
    文件名不符合格式时返回 None（不插入日期）。
    """
    m = re.search(r"report_(\d{8})_\d{6}", report_path.name)
    if not m:
        return None
    d = m.group(1)
    y, mo, day = d[:4], int(d[4:6]), int(d[6:8])
    return f"**报告日期：{y}年{mo}月{day}日**"


class MarkdownToDocx(object):
    """把 Markdown 报告转为 Word 文档。"""

    @staticmethod
    def transfer(
        input_file: str,
        output_file: Optional[str] = None,
        reference_doc: Optional[str] = None,
        include_date: bool = True,
    ) -> str:
        """转换单个 Markdown 文件为 docx。

        Args:
            input_file:   输入的 Markdown 文件路径
            output_file:  输出的 docx 路径（默认与输入同目录、同名）
            reference_doc: Word 样式参考模板路径（可选）
            include_date:  是否在文档最上方插入报告日期（从文件名提取）

        Returns:
            输出的 docx 文件路径
        """
        in_path = Path(input_file)
        if not in_path.exists():
            raise FileNotFoundError(f"输入文件不存在: {in_path}")

        out_path = Path(output_file) if output_file else in_path.with_suffix(".docx")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        extra_args: List[str] = []
        ref = reference_doc or _find_reference_doc(out_path.parent)
        if ref and Path(ref).exists():
            extra_args.append(f"--reference-doc={ref}")

        content = in_path.read_text(encoding="utf-8")
        if include_date:
            date_line = _report_date_line(in_path)
            if date_line:
                content = date_line + "\n\n" + content

        pypandoc.convert_text(
            content,
            "docx",
            format="markdown",
            outputfile=str(out_path),
            extra_args=extra_args,
        )
        return str(out_path)

    @staticmethod
    def transfer_latest() -> str:
        """转换默认输出目录里最新的一份报告。"""
        latest = _find_latest_report()
        if not latest:
            raise FileNotFoundError(
                f"未在 {DEFAULT_OUTPUT_DIR} 找到 report_*.md，请显式传入输入文件"
            )
        return MarkdownToDocx.transfer(str(latest))


if __name__ == "__main__":
    """命令行入口。

    用法:
        python -m tools.word                                    # 最新报告
        python -m tools.word <input.md>                         # 指定输入
        python -m tools.word <input.md> <output.docx>           # 指定输入输出
    """
    args = sys.argv[1:]
    try:
        if len(args) == 0:
            out = MarkdownToDocx.transfer_latest()
            src = str(_find_latest_report())
        elif len(args) == 1:
            src = args[0]
            out = MarkdownToDocx.transfer(src)
        else:
            src = args[0]
            out = MarkdownToDocx.transfer(args[0], args[1])

        print(f"✓ 转换完成: {src}")
        print(f"  → {out}")
    except Exception as e:
        print(f"❌ 转换失败: {e}")
        sys.exit(1)
