"""独立的 MCP Server（HTTP transport），供豆包等 MCP Client 接入。

暴露三个工具：
- google_news：查询新闻/时事
- google_search：一般网页搜索
- export_report：把给定条目导出为 Excel 并返回下载链接

启动：
    python mcp_server.py
豆包「新建自定义连接器」中：
    传输类型：HTTP
    服务器 URL：http://服务器IP:8000/mcp
"""

import json

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from utils.exporter import export_to_excel
from utils.serpapi_tools import google_news as _google_news
from utils.serpapi_tools import google_search as _google_search
from utils.socialcrawl_tools import facebook_profile_posts as _facebook_profile_posts
from utils.settings import MCP_AUTH_TOKEN, MCP_HOST, MCP_PORT

mcp = FastMCP("agno-media", host=MCP_HOST, port=MCP_PORT)


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
def facebook_profile_posts(
    url: str,
    since: str = "",
    until: str = "",
    recent_days: int = 0,
    num: int = 10,
) -> str:
    """抓取指定 Facebook 主页（Profile/Page）的帖子列表，含点赞/评论等互动数据。

    用于查询某个 Facebook 公共主页最近发布的内容。

    Args:
        url: Facebook 主页地址，如 https://www.facebook.com/Meta。
        since: 起始日期（YYYY-MM-DD），只返回该日期之后的帖子，留空不限制。
        until: 截止日期（YYYY-MM-DD），留空不限制。
        recent_days: 仅返回最近 N 天的帖子，0 表示不按天数过滤。
        num: 返回帖子数量上限，默认 10。

    Returns:
        JSON 字符串，含 results(帖子列表) 与 download_url(Excel 下载链接)。
        每条帖子含 title/source/date/link/snippet 及 likes/comments/shares/views/media_url。
    """
    return _facebook_profile_posts(
        url, since=since, until=until, recent_days=recent_days, num=num
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


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """校验 Authorization: Bearer <token>。MCP_AUTH_TOKEN 为空时放行所有请求。"""

    async def dispatch(self, request: Request, call_next):
        if MCP_AUTH_TOKEN:
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
    app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
