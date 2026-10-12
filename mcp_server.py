"""独立的 MCP Server（HTTP transport），供豆包等 MCP Client 接入。

暴露以下工具：
- google_news：查询新闻/时事
- google_search：一般网页搜索
- facebook_search_posts：按关键词搜索 Facebook 公开帖子
- export_report：把给定条目导出为 Excel 并返回下载链接
- latest_daily_report：返回最近一次生成的媒体监控日报 Word 文件
  （只读缓存，不触发流水线；流水线由定时任务每天 13:00 / 16:00 生成，或手动触发）
- trigger_daily_report：手动触发一次日报生成（后台异步 + 加锁，一次只跑一个）

启动：
    python mcp_server.py
豆包「新建自定义连接器」中：
    传输类型：HTTP
    服务器 URL：http://服务器IP:8000/mcp
"""

import json
import mimetypes
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from mcp.types import ResourceLink, TextContent
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from utils.exporter import export_to_excel
from utils.serpapi_tools import google_news as _google_news
from utils.serpapi_tools import google_search as _google_search
from utils.socialcrawl_tools import facebook_search_posts as _facebook_search_posts
from utils.settings import (
    DAILY_REPORT_OUTPUT_DIR,
    DR_SKIP_SCREENSHOT,
    DR_SKIP_SOCIAL,
    DR_SKIP_WORD,
    MCP_AUTH_TOKEN,
    MCP_DOWNLOAD_BASE_URL,
    MCP_HOST,
    MCP_PORT,
)


def _build_download_url(filename: str) -> str:
    """拼日报 .docx 的对外下载链接；配了鉴权 token 时自动带上 ?token=，
    以便文件卡片 / 浏览器点击时也能通过校验（无需手动加 Authorization 头）。
    """
    url = f"{MCP_DOWNLOAD_BASE_URL}/{quote(filename)}"
    if MCP_AUTH_TOKEN:
        url += f"?token={quote(MCP_AUTH_TOKEN)}"
    return url

mcp = FastMCP("agno-media", host=MCP_HOST, port=MCP_PORT)


def _find_latest_daily_report_docx() -> Optional[Path]:
    """在 output/daily_report 下找最新一份 report_*.docx（按文件名时间戳排序）。

    文件名形如 report_YYYYMMDD_HHMMSS.docx，字典序即时间序，取最后一个即最新。
    目录不存在或无 .docx 时返回 None。
    """
    if not DAILY_REPORT_OUTPUT_DIR.exists():
        return None
    docs = sorted(DAILY_REPORT_OUTPUT_DIR.glob("report_*.docx"))
    return docs[-1] if docs else None


# ── 手动触发日报生成（后台异步 + 加锁，一次只跑一个）────────────────
# _gen_lock 保护 _gen_state 的读写；_gen_state 记录当前/最近一次生成任务的状态。
_gen_lock = threading.Lock()
_gen_state = {
    "running": False,  # 是否有生成任务正在后台执行
    "started_at": "",  # 本次/最近一次开始时间
    "finished_at": "",  # 最近一次结束时间
    "last_result": "",  # 最近一次结果摘要（成功 word 路径 / 错误信息）
    "last_ok": None,  # 最近一次是否成功（True/False/None=从未跑过）
}


def _run_pipeline_background() -> None:
    """后台线程执行体：跑完整流水线（仅生成 Word，不推送），结束后更新 _gen_state。

    与定时任务 _run_daily_report_job 行为一致：延迟导入重依赖，吞掉异常避免
    杀掉线程，用 settings 里的 skip 开关控制社交/截图/Word 阶段。
    """
    from utils.daily_report_pipeline import DailyReportPipeline

    ok = False
    summary = ""
    try:
        pipeline = DailyReportPipeline()
        result = pipeline.run_full_pipeline(
            skip_social=DR_SKIP_SOCIAL,
            skip_screenshot=DR_SKIP_SCREENSHOT,
            skip_word=DR_SKIP_WORD,
        )
        if "error" in result:
            summary = f"流水线返回错误: {result['error']}"
        else:
            ok = True
            summary = (
                f"日报已生成: {result.get('word_file') or result.get('report_file', '')}"
            )
    except Exception as e:  # noqa: BLE001 - 后台线程必须吞异常
        summary = f"流水线异常: {e}"
        traceback.print_exc()
    finally:
        with _gen_lock:
            _gen_state["running"] = False
            _gen_state["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _gen_state["last_ok"] = ok
            _gen_state["last_result"] = summary


@mcp.tool()
def google_news(query: str, num: int = 8, gl: str = "us", hl: str = "en") -> str:
    """查询 Google News，用于新闻、时事、最新动态类问题。

    Args:
        query: 搜索关键词。
        num: 返回结果数量上限，默认 8。
        gl: 国家代码（us/cn/uk 等），默认 us。
        hl: 语言代码（en/zh-cn 等），默认 en。

    Returns:
        JSON 字符串，含 results(新闻列表) 与 download_url(Excel 下载链接)。
    """
    return _google_news(query, num=num, gl=gl, hl=hl)


@mcp.tool()
def google_search(query: str, num: int = 8, gl: str = "us", hl: str = "en") -> str:
    """查询 Google 网页搜索，用于一般性、非新闻类问题。

    Args:
        query: 搜索关键词。
        num: 返回结果数量上限，默认 8。
        gl: 国家代码（us/cn/uk 等），默认 us。
        hl: 语言代码（en/zh-cn 等），默认 en。

    Returns:
        JSON 字符串，含 results(网页结果列表) 与 download_url(Excel 下载链接)。
    """
    return _google_search(query, num=num, gl=gl, hl=hl)


@mcp.tool()
def facebook_search_posts(
    query: str,
    recent_posts: bool = True,
    start_date: str = "",
    end_date: str = "",
    max_age_days: int = 0,
    max_pages: int = 1,
    num: int = 10,
) -> str:
    """按关键词搜索 Facebook 公开帖子，含点赞/评论等互动数据。

    用于查询某个关键词/话题在 Facebook 上的公开帖子。

    Args:
        query: 搜索关键词，如 "NASA"、"iPhone 新品"。
        recent_posts: 是否优先返回最近的帖子，默认 True。
        start_date: 起始日期（YYYY-MM-DD），留空不限制。
        end_date: 截止日期（YYYY-MM-DD），留空不限制。
        max_age_days: 仅返回最近 N 天的帖子，0 表示不限制。
        max_pages: 抓取的最大页数，默认 1。
        num: 返回帖子数量上限，默认 10。

    Returns:
        JSON 字符串，含 results(帖子列表) 与 download_url(Excel 下载链接)。
        每条帖子含 title/source/date/link/snippet 及 likes/comments/shares/views/media_url。
    """
    return _facebook_search_posts(
        query,
        recent_posts=recent_posts,
        start_date=start_date,
        end_date=end_date,
        max_age_days=max_age_days,
        max_pages=max_pages,
        num=num,
    )


@mcp.tool()
def export_report(title: str, items: list[dict]) -> str:
    """把一组条目导出为 Excel 报告并返回下载链接。

    Args:
        title: 报告标题（用于文件名）。
        items: 条目列表，每项建议含 title/source/date/link/snippet 字段。

    Returns:
        JSON 字符串，含 download_url(Excel 下载链接)。
    """
    download_url = export_to_excel(title, items)
    return json.dumps({"download_url": download_url}, ensure_ascii=False)


def _human_size(size_bytes: int) -> str:
    """把字节数转成人类可读字符串（B/KB/MB/GB）。"""
    size = float(size_bytes)
    human = f"{size_bytes} B"
    for unit in ("KB", "MB", "GB"):
        if size < 1024:
            break
        size /= 1024
        human = f"{size:.1f} {unit}"
    return human


def _daily_report_meta(path: Path) -> dict:
    """由 .docx 路径构造元信息 dict（供 JSON 文本与 ResourceLink 共用）。"""
    stat = path.stat()

    # 优先用文件名里的时间戳作为「生成时间」（report_YYYYMMDD_HHMMSS）；
    # 解析失败时退回文件修改时间 mtime。
    generated_at = ""
    parts = path.stem.split("_")  # report_YYYYMMDD_HHMMSS
    if len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) == 6:
        try:
            dt = datetime.strptime(parts[1] + parts[2], "%Y%m%d%H%M%S")
            generated_at = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            generated_at = ""
    if not generated_at:
        generated_at = datetime.fromtimestamp(stat.st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    return {
        "ok": True,
        "file_path": str(path.resolve()),
        "file_name": path.name,
        # 对外可访问的 http 下载链接（跨机部署时客户端用它下载）。
        "download_url": _build_download_url(path.name),
        "generated_at": generated_at,
        "size_bytes": stat.st_size,
        "size_human": _human_size(stat.st_size),
    }


# docx 的标准 MIME type
_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@mcp.tool()
def latest_daily_report() -> list:
    """返回最近一次生成的国际媒体监控日报 Word 文件（.docx）。

    只读取缓存：直接返回 output/daily_report 目录下最新一份 report_*.docx，
    不会触发流水线重新抓取/生成。日报由后台定时任务每天下午 13:00 和 16:00
    各生成一次，本工具始终返回其中时间最新的那一份。

    返回富内容（结构化），支持的客户端（如 WorkBuddy）会把日报渲染成**可点击
    的文件卡片 / 下载组件**，而非一段裸文本链接：
    - 一个 ResourceLink：指向该 .docx 的 http 下载链接（MCP 服务自带的下载端点），
      含文件名、大小、类型，客户端据此展示文件卡片，用户点击即可下载 / 打开。
      跨机部署（客户端与 MCP 不同机）也可用，前提是 config.yaml 的
      mcp.public_base_url 填了对外可访问的地址。
    - 一段 TextContent：同样的元信息（JSON，含 download_url），供不渲染资源
      卡片的客户端兜底——可直接把 download_url 渲染成 markdown 链接。

    无缓存时返回仅含一段 TextContent（JSON，ok=false + error 说明）。
    """
    latest = _find_latest_daily_report_docx()
    if latest is None:
        payload = {
            "ok": False,
            "error": (
                "尚未生成任何日报（output/daily_report 下无 report_*.docx）。"
                "日报由定时任务每天 13:00 / 16:00 自动生成，请稍后再试。"
            ),
        }
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

    meta = _daily_report_meta(latest)

    link = ResourceLink(
        type="resource_link",
        # http 下载链接（MCP 服务自带 /download/daily_report 端点）——跨机可用
        uri=meta["download_url"],
        name=meta["file_name"],
        title=f"媒体监控日报 · {meta['generated_at']}",
        description=(
            f"最近一次（{meta['generated_at']}）自动生成的国际媒体监控日报，"
            f"Word 文档，{meta['size_human']}。"
        ),
        mimeType=_DOCX_MIME,
        size=meta["size_bytes"],
    )
    text = TextContent(type="text", text=json.dumps(meta, ensure_ascii=False))
    return [link, text]


@mcp.tool()
def trigger_daily_report() -> str:
    """手动触发一次国际媒体监控日报的生成（抓取 → 挑话题 → 总结 → 渲染 → Word）。

    用于用户希望「现在立刻生成 / 刷新一份最新日报」，而不想等下一次定时任务
    （每天 13:00 / 16:00）。

    行为：
    - **异步**：流水线较重（抓 20+ 网站 + 多次 LLM 调用，通常需数分钟），本工具
      在后台线程启动后**立即返回**，不会阻塞等待跑完。
    - **加锁**：同一时刻只允许一个生成任务。若已有任务在跑，再次调用不会重复
      启动，而是返回「正在生成中」及其开始时间。
    - 生成完成后，用 `latest_daily_report` 获取最新那份 Word（它只读缓存）。

    Returns:
        JSON 字符串：
        - started=true：本次成功启动了后台生成，含 started_at。
        - started=false 且 already_running=true：已有任务在跑，含 started_at。
        附带 last_ok / last_result（最近一次生成的结果摘要，便于判断上次是否成功）。
    """
    with _gen_lock:
        if _gen_state["running"]:
            payload = {
                "started": False,
                "already_running": True,
                "message": "已有日报生成任务正在后台执行，请稍后用 latest_daily_report 获取结果。",
                "started_at": _gen_state["started_at"],
            }
            return json.dumps(payload, ensure_ascii=False)

        # 置为运行中并记录开始时间，再在锁外启动线程
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _gen_state["running"] = True
        _gen_state["started_at"] = now
        _gen_state["finished_at"] = ""

    thread = threading.Thread(
        target=_run_pipeline_background,
        name="daily-report-manual",
        daemon=True,
    )
    thread.start()

    payload = {
        "started": True,
        "already_running": False,
        "message": (
            "已在后台开始生成日报，通常需数分钟。完成后用 latest_daily_report "
            "获取最新 Word 日报。"
        ),
        "started_at": now,
        "last_ok": _gen_state["last_ok"],
        "last_result": _gen_state["last_result"],
    }
    return json.dumps(payload, ensure_ascii=False)


async def download_daily_report(request: Request) -> FileResponse:
    """GET /download/daily_report/{filename}

    从 output/daily_report 目录提供日报 .docx 下载。供 ResourceLink 的 http
    URI 指向，让跨机客户端（WordBuddy 与 MCP 不同机）也能点击下载。

    鉴权：若设置了 MCP_AUTH_TOKEN，需带 Authorization: Bearer <token>，或在
    URL 后加 ?token=<token>（方便浏览器 / 文件卡片直接点击）。
    """
    if MCP_AUTH_TOKEN:
        auth = request.headers.get("authorization", "")
        token_q = request.query_params.get("token", "")
        if auth != f"Bearer {MCP_AUTH_TOKEN}" and token_q != MCP_AUTH_TOKEN:
            return JSONResponse(
                {"error": "Unauthorized: 无效或缺失的 token"}, status_code=401
            )

    filename = request.path_params["filename"]
    filepath = (DAILY_REPORT_OUTPUT_DIR / filename).resolve()
    # 防止路径穿越：必须落在 output/daily_report 目录内，且确为文件
    base = DAILY_REPORT_OUTPUT_DIR.resolve()
    if base not in filepath.parents or not filepath.is_file():
        return JSONResponse({"error": "文件不存在"}, status_code=404)

    media_type, _ = mimetypes.guess_type(filename)
    return FileResponse(
        path=filepath,
        filename=filename,
        media_type=media_type or "application/octet-stream",
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """校验 Authorization: Bearer <token>。MCP_AUTH_TOKEN 为空时放行所有请求。

    /download 路径豁免（其自带 token 校验，支持 header 或 ?token= 查询参数），
    以便文件卡片 / 浏览器点击下载时无需手动加 Authorization 头。
    """

    async def dispatch(self, request: Request, call_next):
        if MCP_AUTH_TOKEN and not request.url.path.startswith("/download/"):
            auth = request.headers.get("authorization", "")
            expected = f"Bearer {MCP_AUTH_TOKEN}"
            if auth != expected:
                return JSONResponse(
                    {"error": "Unauthorized: 无效或缺失的 Authorization 头"},
                    status_code=401,
                )
        return await call_next(request)


if __name__ == "__main__":
    import uvicorn

    app = mcp.streamable_http_app()
    # 注册日报下载端点（供 ResourceLink 的 http URI 指向）
    app.router.routes.append(
        Route(
            "/download/daily_report/{filename}",
            download_daily_report,
            methods=["GET"],
        )
    )
    app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
