"""基于 SocialCrawl 服务的 Facebook 搜索工具封装。

提供工具函数供 Agent / MCP Client 调用：
- facebook_profile_posts：抓取指定 Facebook 主页（Profile/Page）的帖子列表。

API 文档参考：GET https://www.socialcrawl.dev/v1/facebook/profile/posts
鉴权：请求头 x-api-key: <SOCIALCRAWL_API_KEY>
"""

import json
import uuid

import requests

from utils.exporter import export_to_excel
from utils.settings import SOCIALCRAWL_API_KEY, SOCIALCRAWL_BASE_URL

_ENDPOINT = "/v1/facebook/profile/posts"
_TIMEOUT = 60


def _format_posts(items: list[dict]) -> list[dict]:
    """把 SocialCrawl 返回的 items 规整成统一结构。

    统一字段与其它工具保持一致（title/source/date/link/snippet），
    并额外保留社交平台特有的互动数据。
    """
    formatted = []
    for it in items:
        post = it.get("post", {}) or {}
        content = post.get("content", {}) or {}
        author = post.get("author", {}) or {}
        engagement = post.get("engagement", {}) or {}

        text = content.get("text", "") or ""
        snippet = text[:200]

        formatted.append(
            {
                "title": snippet or "(无文本内容)",
                "source": author.get("display_name", "") or "",
                "date": post.get("published_at", "") or "",
                "link": post.get("url", "") or "",
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


def facebook_profile_posts(
    url: str,
    since: str = "",
    until: str = "",
    recent_days: int = 0,
    include: str = "engagement",
    num: int = 10,
) -> str:
    """抓取指定 Facebook 主页的帖子列表。

    Args:
        url: Facebook 主页/公共主页地址，如 https://www.facebook.com/Meta。
        since: 起始日期（YYYY-MM-DD），只返回该日期之后的帖子。留空表示不限制。
        until: 截止日期（YYYY-MM-DD）。留空表示不限制。
        recent_days: 仅返回最近 N 天的帖子（0 表示不按天数过滤）。
        include: 附加返回的数据，默认 engagement（包含点赞/评论等互动数据）。
        num: 返回的帖子数量上限，默认 10。

    Returns:
        JSON 字符串，含 results(帖子列表) 与 download_url(Excel 下载链接)。
        每条帖子含 title/source/date/link/snippet 及 likes/comments/shares/views/media_url。
    """
    if not SOCIALCRAWL_API_KEY:
        return json.dumps(
            {"error": "未配置 SOCIALCRAWL_API_KEY，请在 .env 中设置。"},
            ensure_ascii=False,
        )

    params = {
        "url": url,
        "include": include,
    }
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if recent_days:
        params["recent_days"] = recent_days

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
    if not data.get("success", False):
        return json.dumps(
            {"error": f"SocialCrawl 返回失败：{data.get('error', 'unknown')}"},
            ensure_ascii=False,
        )

    items = (data.get("data", {}) or {}).get("items", [])
    results = _format_posts(items)[:num]
    download_url = export_to_excel(f"facebook_{_safe_slug(url)}", results)
    return json.dumps(
        {"results": results, "download_url": download_url},
        ensure_ascii=False,
    )


def _safe_slug(url: str) -> str:
    """从主页 URL 取一个简短标识用于文件名。"""
    tail = url.rstrip("/").split("/")[-1]
    return tail or "profile"
