"""将搜索/新闻结果导出为 Excel 报告，并返回可下载的 URL。"""

import re
import time

from openpyxl import Workbook
from openpyxl.styles import Font

from utils.settings import DOWNLOAD_BASE_URL, EXPORT_DIR

_COLUMNS = [
    ("title", "标题"),
    ("source", "来源"),
    ("date", "时间"),
    ("link", "链接"),
    ("snippet", "摘要"),
]


def _safe_name(query: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", query).strip("_")[:30]
    return slug or "report"


def export_to_excel(query: str, results: list[dict]) -> str:
    """把结果列表写入 xlsx 文件，返回对外下载 URL。

    Args:
        query: 本次搜索关键词，用于文件名和表头。
        results: 结果列表，每项含 title/source/date/link/snippet。

    Returns:
        可点击下载的 URL 字符串。
    """
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    filename = f"{_safe_name(query)}_{int(time.time())}.xlsx"
    filepath = EXPORT_DIR / filename

    wb = Workbook()
    ws = wb.active
    ws.title = "结果"

    headers = [label for _, label in _COLUMNS]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for r in results:
        ws.append([str(r.get(key, "")) for key, _ in _COLUMNS])

    widths = [40, 18, 22, 50, 60]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + idx)].width = width

    wb.save(filepath)
    return f"{DOWNLOAD_BASE_URL}/{filename}"
