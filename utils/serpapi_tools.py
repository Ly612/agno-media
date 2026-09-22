"""基于 SerpAPI 的 Google 搜索/新闻工具封装。

提供两个工具函数供 Agent 调用：
- google_news：查询与新闻/时事相关的内容（engine=google_news）
- google_search：查询一般性网页内容（engine=google，默认）

由 LLM 根据用户提问自动选择调用哪个。
"""

import json

from serpapi import GoogleSearch

from utils.exporter import export_to_excel
from utils.settings import SERPAPI_API_KEY


def _format_news(results: list[dict]) -> list[dict]:
    items = []
    for r in results:
        items.append(
            {
                "title": r.get("title", ""),
                "source": (r.get("source") or {}).get("name", "")
                if isinstance(r.get("source"), dict)
                else r.get("source", ""),
                "date": r.get("date", ""),
                "link": r.get("link", ""),
                "snippet": r.get("snippet", ""),
            }
        )
    return items


def _format_organic(results: list[dict]) -> list[dict]:
    items = []
    for r in results:
        items.append(
            {
                "title": r.get("title", ""),
                "source": r.get("displayed_link", ""),
                "date": r.get("date", ""),
                "link": r.get("link", ""),
                "snippet": r.get("snippet", ""),
            }
        )
    return items


def google_news(query: str, num: int = 8, gl: str = "us", hl: str = "en") -> str:
    """查询 Google News，用于与新闻、时事、最新动态相关的问题。

    Args:
        query: 搜索关键词。
        num: 返回的结果数量上限，默认 8。
        gl: 国家代码（如 us、cn、uk），默认 us。
        hl: 语言代码（如 en、zh-cn），默认 en。

    Returns:
        JSON 字符串，包含新闻列表，每项含 title/source/date/link/snippet。
    """
    if not SERPAPI_API_KEY:
        return json.dumps({"error": "未配置 SERPAPI_API_KEY，请在 .env 中设置。"}, ensure_ascii=False)

    search = GoogleSearch(
        {
            "engine": "google_news",
            "q": query,
            "gl": gl,
            "hl": hl,
            "api_key": SERPAPI_API_KEY,
        }
    )
    data = search.get_dict()
    if "error" in data:
        return json.dumps({"error": data["error"]}, ensure_ascii=False)
    results = _format_news(data.get("news_results", []))[:num]
    download_url = export_to_excel(query, results)
    return json.dumps({"results": results, "download_url": download_url}, ensure_ascii=False)


def google_search(query: str, num: int = 8, gl: str = "us", hl: str = "en") -> str:
    """查询 Google 网页搜索，用于一般性、非新闻类的问题。

    Args:
        query: 搜索关键词。
        num: 返回的结果数量上限，默认 8。
        gl: 国家代码（如 us、cn、uk），默认 us。
        hl: 语言代码（如 en、zh-cn），默认 en。

    Returns:
        JSON 字符串，包含网页结果列表，每项含 title/source/date/link/snippet。
    """
    if not SERPAPI_API_KEY:
        return json.dumps({"error": "未配置 SERPAPI_API_KEY，请在 .env 中设置。"}, ensure_ascii=False)

    search = GoogleSearch(
        {
            "engine": "google",
            "q": query,
            "gl": gl,
            "hl": hl,
            "num": num,
            "api_key": SERPAPI_API_KEY,
        }
    )
    data = search.get_dict()
    if "error" in data:
        return json.dumps({"error": data["error"]}, ensure_ascii=False)
    results = _format_organic(data.get("organic_results", []))[:num]
    download_url = export_to_excel(query, results)
    return json.dumps({"results": results, "download_url": download_url}, ensure_ascii=False)
