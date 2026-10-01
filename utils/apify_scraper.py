"""社交平台抓取工具：通过 Apify API 抓取 Facebook / X / Google News 的关键字数据。

纯抓取层，不包含 LLM Agent 逻辑。贴文挑选、内容总结、简报渲染等请使用
agents.media_monitor_agent.MediaMonitorAgent。

三个 Apify actor：
    - Facebook:    danek/facebook-search-ppr          (l6CUZt8H0214D3I0N)
    - X:           api-ninja/x-twitter-advanced-search (0oVSlMlAX47R2EyoP)
    - Google News: data_xplorer/google-news-scraper-fast (3Z6SK7F2WoPU3t2sg)

依赖:
    - requests
    - Apify API key（环境变量 APIFY_API_KEY）

用法:
    from utils.apify_scraper import ApifyScraperToolkit

    scraper = ApifyScraperToolkit()
    result = scraper.scrape_all_social()          # 三个平台全部抓取
    fb = scraper.scrape_facebook(keyword="China")  # 单个平台
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from agno.tools import Toolkit, tool

from utils.settings import apify_settings

# ── 常量 ──────────────────────────────────────────────────
BEIJING_TZ = timezone(timedelta(hours=8))
APIFY_ENDPOINT = "https://api.apify.com/v2"
DEFAULT_OUTPUT_DIR = Path(__file__).parent.parent / "output" / "media_monitor" / "social"

# Apify actor ID（与 console.apify.com/actors/{id}/input 对应）
FACEBOOK_ACTOR_ID = "l6CUZt8H0214D3I0N"
X_ACTOR_ID = "0oVSlMlAX47R2EyoP"
GOOGLE_NEWS_ACTOR_ID = "3Z6SK7F2WoPU3t2sg"

MAX_RESULTS = 200       # Facebook / X 默认最多条数
MAX_AGE_DAYS = 1        # 默认限制最近一天
STORE_TEXT_LIMIT = 2000  # 落盘时单条贴文文本截断长度


def _to_datetime(value: Any) -> Optional[datetime]:
    """把 actor 返回的日期转成带时区的 datetime（无法解析返回 None）。

    兼容 ISO 字符串、数字时间戳（秒/毫秒）、'YYYY-MM-DD'、
    Twitter 风格 'Thu Aug 06 07:53:04 +0000 2026' 等格式。
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            v = float(value)
            if v > 1e12:  # 毫秒
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S +0000 %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _first(item: dict, *keys: str, default: Any = None) -> Any:
    """按候选字段名顺序取第一个非空值（actor 输出字段名容错）。"""
    for k in keys:
        v = item.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _extract_urls(text: Any) -> List[str]:
    """从贴文文本中提取所有 http(s) 链接（去重、去尾随标点）。

    用于捕获贴文内引用的原始文章链接（如 X 的 t.co 短链、FB 附带的文章 URL）。
    """
    if not text:
        return []
    seen: List[str] = []
    for m in re.findall(r"https?://[^\s<>\"']+", str(text)):
        u = m.rstrip(".,;:!?)]}，。；：！？")
        if u and u not in seen:
            seen.append(u)
    return seen


# ═══════════════════════════════════════════════════════════
# ApifyScraperToolkit（纯抓取层）
# ═══════════════════════════════════════════════════════════

class ApifyScraperToolkit(Toolkit):
    """社交平台抓取工具：通过 Apify API 抓取 Facebook / X / Google News。

    统一输出归一化后的贴文结构：
        {platform, author_name, author_handle, followers, text, url,
         published_at, engagement, media, article_urls}
    其中 media 取值为 video / image / none；
    article_urls 为贴文正文中引用的文章链接列表（含 t.co 短链等），用于报告里直列原始文章链接。

    Args:
        apify_api_key: Apify API 密钥（默认从 config.apify_settings 读取，env: APIFY_API_KEY）
        output_dir:    输出目录（默认项目根/output/media_monitor/social）
    """

    def __init__(
        self,
        apify_api_key: Optional[str] = None,
        output_dir: Optional[str] = None,
    ):
        super().__init__(name="apify_scraper")

        self.apify_api_key = apify_api_key or apify_settings.api_key
        self.output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
        # 本地代理（config.APIFY_PROXY，可被 .env 覆盖），直连 api.apify.com 失败时兜底
        self.proxy_url = apify_settings.proxy

        self.register(self.scrape_facebook)
        self.register(self.scrape_x)
        self.register(self.scrape_google_news)
        self.register(self.scrape_all_social)

    # ── 底层 API 调用 ──────────────────────────────────────

    def _request_with_proxy_fallback(
        self, method: str, url: str, **kwargs
    ) -> requests.Response:
        """请求 Apify API：直连失败时自动走本地代理重试。

        遵循项目网络代理规则：优先直连，遇到连接类异常
        （SSLError / ConnectionError / Timeout 等）才加代理兜底。
        """
        timeout = kwargs.pop("timeout", 60)
        try:
            return requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}
            return requests.request(
                method, url, timeout=timeout, proxies=proxies, **kwargs
            )

    def _run_actor_and_get_items(
        self,
        actor_id: str,
        input_data: dict,
        max_results: int,
        max_wait: int = 600,
    ) -> tuple:
        """启动 Apify actor 运行，等待完成并读取结果数据集。

        Returns:
            (items, error): items 为原始数据集列表，error 为失败信息
        """
        if not self.apify_api_key:
            return None, "APIFY_API_KEY 未设置"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.apify_api_key}",
        }

        # 1. 启动运行
        try:
            resp = self._request_with_proxy_fallback(
                "POST",
                f"{APIFY_ENDPOINT}/acts/{actor_id}/runs",
                headers=headers, json=input_data, timeout=60,
            )
        except requests.RequestException as e:
            return None, f"启动运行请求失败: {e}"
        if resp.status_code not in (200, 201):
            return None, f"启动运行失败 HTTP {resp.status_code}: {resp.text[:300]}"

        run_id = resp.json().get("data", {}).get("id")
        if not run_id:
            return None, f"启动运行未返回 run id: {resp.text[:300]}"

        # 2. 轮询运行状态
        status = "READY"
        dataset_id = None
        deadline = time.time() + max_wait
        while status in ("READY", "RUNNING", "WAITING"):
            if time.time() > deadline:
                return None, f"运行超时（>{max_wait}s），run_id={run_id}"
            time.sleep(5)
            try:
                r = self._request_with_proxy_fallback(
                    "GET",
                    f"{APIFY_ENDPOINT}/actor-runs/{run_id}",
                    headers=headers, timeout=60,
                )
            except requests.RequestException as e:
                return None, f"查询运行状态请求失败: {e}"
            if r.status_code != 200:
                return None, f"查询运行状态失败 HTTP {r.status_code}: {r.text[:200]}"
            run = r.json().get("data", {})
            status = run.get("status", "READY")
            dataset_id = run.get("defaultDatasetId")

        if status != "SUCCEEDED":
            return None, f"运行未成功（status={status}），run_id={run_id}"
        if not dataset_id:
            return None, f"运行成功但无数据集，run_id={run_id}"

        # 3. 读取数据集
        params = {"format": "json", "limit": min(max_results, 10000), "offset": 0}
        try:
            r = self._request_with_proxy_fallback(
                "GET",
                f"{APIFY_ENDPOINT}/datasets/{dataset_id}/items",
                headers=headers, params=params, timeout=120,
            )
        except requests.RequestException as e:
            return None, f"读取数据集请求失败: {e}"
        if r.status_code != 200:
            return None, f"读取数据集失败 HTTP {r.status_code}: {r.text[:300]}"

        items = r.json()
        if not isinstance(items, list):
            return None, f"数据集格式异常: {str(items)[:200]}"
        return items, None

    # ── actor 输入构造（字段名待用真实 schema 对账）─────────

    def _build_facebook_input(
        self, keyword: str, search_type: str, max_results: int, max_age_days: int,
    ) -> dict:
        """facebook-search-ppr 输入（已按真实 input-schema 对账）。

        字段：search_type / query / max_posts。日期限制由抓取后的 _filter_by_age 兜底。
        """
        return {
            "search_type": search_type,   # posts / groups / pages / events / places / all
            "query": keyword,
            "max_posts": max_results,
        }

    def _build_x_input(
        self, keyword: str, search_type: str, max_results: int, max_age_days: int,
    ) -> dict:
        """x-twitter-advanced-search 输入（已按真实 input-schema 对账）。

        字段：query / search_type / numberOfTweets / timeWithinTime。
        numberOfTweets 最小 20，实际取 max(20, max_results)，保存时再截断回 max_results。
        """
        return {
            "query": keyword,
            "search_type": search_type,   # Top / Latest / Media / People
            "numberOfTweets": max(20, max_results),
            "timeWithinTime": f"{max_age_days}d",
        }

    def _build_google_news_input(
        self, keyword: str, max_results: int, max_age_days: int,
    ) -> dict:
        """google-news-scraper-fast 输入（已按真实 input-schema 对账）。

        字段：keywords(数组) / maxArticles / timeframe / region_language / decodeUrls 等。
        """
        return {
            "keywords": [keyword],
            "maxArticles": max_results,
            "timeframe": f"{max_age_days}d",
            "region_language": "US:en",
            "decodeUrls": True,       # 把 Google 重定向解析为原始文章 URL
            "extractDescriptions": True,
            "extractImages": True,
        }

    # ── 归一化 ─────────────────────────────────────────────

    def _normalize_facebook(self, items: List[dict], keyword: str) -> List[dict]:
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            post_url = _first(it, "url")
            text = _first(it, "message", "text", "caption", "description", default="")
            if not text and not post_url:
                continue
            author = it.get("author") or {}
            reactions = it.get("reactions_count") or 0
            comments = it.get("comments_count") or 0
            shares = it.get("reshare_count") or 0
            images_count = it.get("images_count") or 0
            published = _first(it, "timestamp", "publishedAt", "postTime")
            out.append({
                "platform": "Facebook",
                "keyword": keyword,
                "author_name": _first(author, "name", default="") if isinstance(author, dict) else str(author),
                "author_handle": "",   # 该 actor 输出无账号 handle
                "followers": None,
                "text": str(text)[:STORE_TEXT_LIMIT],
                "url": str(post_url) if post_url else "",
                "published_at": published,
                "_published_dt": _to_datetime(published),
                "engagement": int(reactions) + int(comments) + int(shares),
                "media": "video" if it.get("video") else (
                    "image" if (it.get("image") or images_count) else "none"),
                "article_urls": _extract_urls(text),
            })
        return out

    def _detect_x_media(self, media: Any) -> str:
        """X 的 media 字段是 {类型: [条目...]} 字典，按 key 判断视频/图片。"""
        if not media:
            return "none"
        if isinstance(media, dict):
            keys = {str(k).lower() for k in media.keys()}
            if keys & {"video", "animated_gif", "player", "promo_video_convo"}:
                return "video"
            if keys & {"photo", "image", "summary_large_image", "promo_image_app",
                       "promo_image_convo", "promo_website"}:
                return "image"
            return "none"
        return "image" if isinstance(media, list) and media else "none"

    def _normalize_x(self, items: List[dict], keyword: str) -> List[dict]:
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            text = _first(it, "text", "fullText", "content", default="")
            url = _first(it, "url", default="")
            if not text and not url:
                continue
            user_info = it.get("user_info") or {}
            screen_name = _first(it, "screen_name", "username", default="")
            if not screen_name and isinstance(user_info, dict):
                screen_name = _first(user_info, "screen_name", "username", default="")
            tweet_id = _first(it, "tweet_id", "id_str", default="")
            # 优先构造 x.com 链接，匹配报告示例格式
            if screen_name and tweet_id:
                url = f"https://x.com/{screen_name}/status/{tweet_id}"
            elif not url:
                url = f"https://twitter.com/{screen_name}/status/{tweet_id}"
            likes = it.get("favorites") or 0
            retweets = it.get("retweets") or 0
            replies = it.get("replies") or 0
            quotes = it.get("quotes") or 0
            bookmarks = it.get("bookmarks") or 0
            published = _first(it, "created_at", "date", "timestamp")
            out.append({
                "platform": "X",
                "keyword": keyword,
                "author_name": _first(user_info, "name", default="") if isinstance(user_info, dict) else str(user_info),
                "author_handle": str(screen_name),
                "followers": _first(user_info, "followers_count", default=None) if isinstance(user_info, dict) else None,
                "text": str(text)[:STORE_TEXT_LIMIT],
                "url": str(url),
                "published_at": published,
                "_published_dt": _to_datetime(published),
                "engagement": int(likes) + int(retweets) + int(replies) + int(quotes) + int(bookmarks),
                "media": self._detect_x_media(it.get("media")),
                "article_urls": _extract_urls(text),
            })
        return out

    def _normalize_google_news(self, items: List[dict], keyword: str) -> List[dict]:
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = _first(it, "title", default="")
            desc = _first(it, "description", "snippet", default="") or ""
            if not title and not desc:
                continue
            source = it.get("source") or {}
            source_name = source.get("name", "") if isinstance(source, dict) else str(source)
            link = _first(it, "link", "url", default="")
            published = _first(it, "publishedAt", "date", "timestamp", "time")
            full_text = str(title)
            if desc:
                full_text = f"{title}. {desc}" if title else str(desc)
            out.append({
                "platform": "Google News",
                "keyword": keyword,
                "author_name": source_name,
                "author_handle": "",
                "followers": None,
                "text": full_text[:STORE_TEXT_LIMIT],
                "url": str(link),
                "published_at": published,
                "_published_dt": _to_datetime(published),
                "engagement": 0,
                "media": "image" if it.get("image") else "none",
                "article_urls": _extract_urls(full_text),
            })
        return out

    # ── 日期过滤 ───────────────────────────────────────────

    def _filter_by_age(self, items: List[dict], max_age_days: int) -> List[dict]:
        """只保留最近 max_age_days 天内的贴文（按 published_at 过滤）。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        kept = []
        for it in items:
            dt = it.get("_published_dt")
            if dt is None or dt >= cutoff:
                kept.append(it)
        return kept

    def _finalize(self, items: List[dict]) -> List[dict]:
        """落盘前去内部字段。"""
        return [{k: v for k, v in it.items() if k != "_published_dt"} for it in items]

    def _save_platform(
        self,
        platform: str,
        items: List[dict],
        max_results: int,
        max_age_days: int,
    ) -> Dict[str, Any]:
        """归一化 → 日期过滤 → 截断条数 → 落盘。"""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        filtered = self._filter_by_age(items, max_age_days)[:max_results]
        payload = {
            "platform": platform,
            "keyword": "China",
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "max_age_days": max_age_days,
            "count": len(filtered),
            "items": self._finalize(filtered),
        }
        file_name = {"Facebook": "facebook", "X": "x", "Google News": "google_news"}[platform]
        out_file = self.output_dir / f"{file_name}.json"
        out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "ok", "platform": platform, "count": len(filtered), "file": str(out_file)}

    # ── 平台抓取实现（CLI / 内部复用）──────────────────────

    def _scrape_facebook_impl(
        self,
        keyword: str = "China",
        search_type: str = "posts",
        max_results: int = MAX_RESULTS,
        max_age_days: int = MAX_AGE_DAYS,
    ) -> Dict[str, Any]:
        print(f"[Facebook] 搜索 '{keyword}' (type={search_type}, max={max_results}) ...", flush=True)
        input_data = self._build_facebook_input(keyword, search_type, max_results, max_age_days)
        items, err = self._run_actor_and_get_items(FACEBOOK_ACTOR_ID, input_data, max_results)
        if err:
            print(f"   ✗ {err}")
            return {"status": "error", "error": err}
        normalized = self._normalize_facebook(items, keyword)
        result = self._save_platform("Facebook", normalized, max_results, max_age_days)
        print(f"   ✓ 原始 {len(items)} 条 → 过滤后 {result['count']} 条")
        return result

    def _scrape_x_impl(
        self,
        keyword: str = "China",
        search_type: str = "Top",
        max_results: int = MAX_RESULTS,
        max_age_days: int = MAX_AGE_DAYS,
    ) -> Dict[str, Any]:
        print(f"[X] 搜索 '{keyword}' (type={search_type}, max={max_results}) ...", flush=True)
        input_data = self._build_x_input(keyword, search_type, max_results, max_age_days)
        items, err = self._run_actor_and_get_items(X_ACTOR_ID, input_data, max_results)
        if err:
            print(f"   ✗ {err}")
            return {"status": "error", "error": err}
        normalized = self._normalize_x(items, keyword)
        result = self._save_platform("X", normalized, max_results, max_age_days)
        print(f"   ✓ 原始 {len(items)} 条 → 过滤后 {result['count']} 条")
        return result

    def _scrape_google_news_impl(
        self,
        keyword: str = "China",
        max_results: int = 50,
        max_age_days: int = MAX_AGE_DAYS,
    ) -> Dict[str, Any]:
        print(f"[Google News] 搜索 '{keyword}' (max={max_results}) ...", flush=True)
        input_data = self._build_google_news_input(keyword, max_results, max_age_days)
        items, err = self._run_actor_and_get_items(GOOGLE_NEWS_ACTOR_ID, input_data, max_results)
        if err:
            print(f"   ✗ {err}")
            return {"status": "error", "error": err}
        normalized = self._normalize_google_news(items, keyword)
        result = self._save_platform("Google News", normalized, max_results, max_age_days)
        print(f"   ✓ 原始 {len(items)} 条 → 过滤后 {result['count']} 条")
        return result

    def _scrape_all_social_impl(
        self,
        keyword: str = "China",
        facebook_type: str = "posts",
        x_type: str = "Top",
        facebook_max: int = MAX_RESULTS,
        x_max: int = MAX_RESULTS,
        google_news_max: int = 50,
        max_age_days: int = MAX_AGE_DAYS,
    ) -> Dict[str, Any]:
        """依次抓取三个平台，汇总写 social_manifest.json。"""
        results = {
            "Facebook": self._scrape_facebook_impl(keyword, facebook_type, facebook_max, max_age_days),
            "X": self._scrape_x_impl(keyword, x_type, x_max, max_age_days),
            "Google News": self._scrape_google_news_impl(keyword, google_news_max, max_age_days),
        }
        manifest = {
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "keyword": keyword,
            "results": results,
        }
        self.output_dir.mkdir(parents=True, exist_ok=True)
        manifest_file = self.output_dir / "social_manifest.json"
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        ok_count = sum(1 for r in results.values() if r.get("status") == "ok")
        print(f"\n📊 社交平台抓取完成: {ok_count}/3 成功")
        return {
            "ok": ok_count,
            "total": 3,
            "results": results,
            "dir": str(self.output_dir),
            "manifest_file": str(manifest_file),
        }

    # ── 注册为 Agent 工具的方法 ─────────────────────────────

    @tool(show_result=True)
    def scrape_facebook(
        self,
        keyword: str = "China",
        search_type: str = "posts",
        max_results: int = MAX_RESULTS,
        max_age_days: int = MAX_AGE_DAYS,
    ) -> Dict[str, Any]:
        """抓取 Facebook 上关键字相关的帖子。

        Args:
            keyword:      搜索关键字，默认 "China"
            search_type:  搜索类型，默认 "posts"
            max_results:  最多抓取条数，默认 200
            max_age_days: 限制最近 N 天，默认 1

        Returns:
            {"status": "ok", "count": N, "file": "..."} 或 {"status": "error", "error": "..."}
        """
        return self._scrape_facebook_impl(keyword, search_type, max_results, max_age_days)

    @tool(show_result=True)
    def scrape_x(
        self,
        keyword: str = "China",
        search_type: str = "Top",
        max_results: int = MAX_RESULTS,
        max_age_days: int = MAX_AGE_DAYS,
    ) -> Dict[str, Any]:
        """抓取 X (Twitter) 上关键字相关的帖子。

        Args:
            keyword:      搜索关键字，默认 "China"
            search_type:  排序类型，默认 "Top"
            max_results:  最多抓取条数，默认 200
            max_age_days: 限制最近 N 天，默认 1

        Returns:
            {"status": "ok", "count": N, "file": "..."} 或 {"status": "error", "error": "..."}
        """
        return self._scrape_x_impl(keyword, search_type, max_results, max_age_days)

    @tool(show_result=True)
    def scrape_google_news(
        self,
        keyword: str = "China",
        max_results: int = 50,
        max_age_days: int = MAX_AGE_DAYS,
    ) -> Dict[str, Any]:
        """抓取 Google News 上关键字相关的新闻。

        注意：该 actor 按抓取条数计费（Pay Per Event），默认 50 条。

        Args:
            keyword:      搜索关键字，默认 "China"
            max_results:  最多抓取条数，默认 50
            max_age_days: 限制最近 N 天，默认 1

        Returns:
            {"status": "ok", "count": N, "file": "..."} 或 {"status": "error", "error": "..."}
        """
        return self._scrape_google_news_impl(keyword, max_results, max_age_days)

    @tool(show_result=True)
    def scrape_all_social(
        self,
        keyword: str = "China",
        max_age_days: int = MAX_AGE_DAYS,
        facebook_max: int = MAX_RESULTS,
        x_max: int = MAX_RESULTS,
        google_news_max: int = 50,
    ) -> Dict[str, Any]:
        """一键抓取 Facebook / X / Google News 三个平台关键字相关数据。

        Args:
            keyword:       搜索关键字，默认 "China"
            max_age_days:  限制最近 N 天，默认 1
            facebook_max:  Facebook 最多条数，默认 200
            x_max:         X 最多条数，默认 200
            google_news_max: Google News 最多条数，默认 50

        Returns:
            {"ok": N, "total": 3, "results": {...}, "dir": "...", "manifest_file": "..."}
        """
        return self._scrape_all_social_impl(
            keyword=keyword,
            facebook_max=facebook_max,
            x_max=x_max,
            google_news_max=google_news_max,
            max_age_days=max_age_days,
        )


# ═══════════════════════════════════════════════════════════
# 命令行入口（仅测试抓取功能）
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    """直接运行时测试抓取功能。

    用法:
        python -m tools.apify_scraper            # 抓取全部三个平台
        python -m tools.apify_scraper facebook   # 只抓 Facebook
        python -m tools.apify_scraper x          # 只抓 X
        python -m tools.apify_scraper google_news  # 只抓 Google News
    """
    import sys

    scraper = ApifyScraperToolkit()
    if not scraper.apify_api_key:
        print("❌ APIFY_API_KEY 未设置（请设置 APIFY_API_KEY 环境变量）")
        sys.exit(1)

    platform = sys.argv[1] if len(sys.argv) > 1 else "all"
    if platform == "facebook":
        result = scraper._scrape_facebook_impl()
    elif platform == "x":
        result = scraper._scrape_x_impl()
    elif platform == "google_news":
        result = scraper._scrape_google_news_impl()
    else:
        result = scraper._scrape_all_social_impl()

    print(json.dumps(result, ensure_ascii=False, indent=2))
