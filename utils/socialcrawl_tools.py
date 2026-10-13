"""基于 SocialCrawl 服务的 Facebook 帖子搜索工具封装。

提供工具函数供 Agent / MCP Client 调用：
- facebook_search_posts：按关键词搜索 Facebook 公开帖子。

API 参考：GET https://www.socialcrawl.dev/v1/facebook/search/posts
鉴权：请求头 x-api-key: <SOCIALCRAWL_API_KEY>
"""

import json
import uuid

import requests

from utils.exporter import export_to_excel
from utils.settings import SOCIALCRAWL_API_KEY, SOCIALCRAWL_BASE_URL

_ENDPOINT = "/v1/facebook/search/posts"
_TIMEOUT = 120
# 服务端单次调用支持的最大页数（官方限制 1-5）
_MAX_PAGES_CAP = 5
# 估算每页约多少条，用于根据 num 推算需要的页数
_EST_PER_PAGE = 6


def _format_posts(items: list[dict]) -> list[dict]:
    """把 SocialCrawl 返回的 items 规整成统一结构。

    统一字段与其它工具保持一致（title/source/date/link/snippet），
    并额外保留社交平台特有的互动数据。对返回结构做兜底，字段缺失不报错。
    """
    formatted = []
    for it in items:
        post = it.get("post", it) if isinstance(it, dict) else {}
        content = post.get("content", {}) or {}
        author = post.get("author", {}) or {}
        engagement = post.get("engagement", {}) or {}

        text = content.get("text", "") or post.get("text", "") or ""
        snippet = text[:200]

        formatted.append(
            {
                "title": snippet or "(无文本内容)",
                "source": author.get("display_name", "") or author.get("name", "") or "",
                "date": post.get("published_at", "") or post.get("created_time", "") or "",
                "link": post.get("url", "") or post.get("permalink", "") or "",
                "snippet": snippet,
                # 社交平台特有字段
                "likes": engagement.get("likes"),
                "comments": engagement.get("comments"),
                "shares": engagement.get("shares"),
                "views": engagement.get("views"),
                "media_url": content.get("media_urls", "") or "",
            }
        )
    return formatted


def _dedupe_items(items: list[dict]) -> list[dict]:
    """按 post.id 去重（跨页重复是 Facebook 搜索的正常行为，非分页 bug）。"""
    seen_ids: set = set()
    unique: list[dict] = []
    for it in items:
        post = it.get("post", it) if isinstance(it, dict) else {}
        pid = post.get("id") or post.get("url") or post.get("permalink")
        if pid is None:
            unique.append(it)
            continue
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        unique.append(it)
    return unique


def facebook_search_posts(
    query: str,
    recent_posts: bool = True,
    start_date: str = "",
    end_date: str = "",
    max_age_days: int = 0,
    max_pages: int = 0,
    num: int = 10,
) -> str:
    """按关键词搜索 Facebook 公开帖子。

    使用服务端原生多页抓取（max_pages 1-5），自动按 post.id 去重，
    并用 seen 参数让重复页免费、节省 credits。

    Args:
        query: 搜索关键词，如 "NASA"、"iPhone 新品"。
        recent_posts: 为 True 时按时间排序（最新优先），False 按相关度。默认 True。
        start_date: 起始日期（YYYY-MM-DD），留空不限制。
        end_date: 截止日期（YYYY-MM-DD），留空不限制。
        max_age_days: 仅返回最近 N 天的帖子（1-3650，0 表示不限制）。
        max_pages: 抓取页数（1-5）。0（默认）表示根据 num 自动推算，上限 5 页。
        num: 返回帖子数量上限，默认 10。

    Returns:
        JSON 字符串，含 results(帖子列表) 与 download_url(Excel 下载链接)。
        每条帖子含 title/source/date/link/snippet 及 likes/comments/shares/views/media_url。
    """
    if not SOCIALCRAWL_API_KEY:
        return json.dumps(
            {"error": "未配置 SOCIALCRAWL_API_KEY，请在 .env 中设置。"},
            ensure_ascii=False,
        )

    # 根据 num 推算页数（每页约 _EST_PER_PAGE 条），限制在 1-5
    if max_pages <= 0:
        pages = max(1, -(-num // _EST_PER_PAGE))  # ceil(num / _EST_PER_PAGE)
    else:
        pages = max_pages
    pages = min(max(pages, 1), _MAX_PAGES_CAP)

    params = {
        "query": query,
        "recent_posts": "true" if recent_posts else "false",
        "max_pages": pages,
        # seen：同一查询 24h 内自动去重，重复页免费
        "seen": f"fb-{_safe_slug(query)}",
    }
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if max_age_days:
        params["max_age_days"] = max_age_days

    headers = {
        "Cache-Control": "no-cache",
        "Idempotency-Key": str(uuid.uuid4()),
        "x-api-key": SOCIALCRAWL_API_KEY,
    }

    try:
        resp = requests.get(
            f"{SOCIALCRAWL_BASE_URL}{_ENDPOINT}",
            params=params,
            headers=headers,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        return json.dumps({"error": f"请求 SocialCrawl 失败：{e}"}, ensure_ascii=False)

    if resp.status_code != 200:
        return json.dumps(
            {"error": f"SocialCrawl 返回 {resp.status_code}: {resp.text[:300]}"},
            ensure_ascii=False,
        )

    data = resp.json()
    if not data.get("success", True) and data.get("error"):
        return json.dumps(
            {"error": f"SocialCrawl 返回失败：{data.get('error')}"},
            ensure_ascii=False,
        )

    items = (data.get("data", {}) or {}).get("items", [])
    items = _dedupe_items(items)
    results = _format_posts(items)[:num]
    download_url = export_to_excel(f"facebook_search_{_safe_slug(query)}", results)
    return json.dumps(
        {"results": results, "download_url": download_url},
        ensure_ascii=False,
    )


def _safe_slug(text: str) -> str:
    """从关键词取一个简短标识用于文件名。"""
    tail = "".join(c for c in text if c.isalnum())[:20]
    return tail or "search"
