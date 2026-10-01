"""媒体监控抓取工具：调用 BrightData API 批量抓取 23 家国际主流媒体首页。

纯抓取层，不包含 LLM Agent 逻辑。话题挑选、摘要生成、简报渲染等请使用
agents.media_monitor_agent.MediaMonitorAgent。

依赖:
    - requests
    - BrightData API key（环境变量 BRIGHTDATA_API_KEY / BRIGHTDATA_ZONE）

用法:
    from utils.media_monitor_tool import MediaMonitorToolkit

    scraper = MediaMonitorToolkit()
    result = scraper.scrape_all_homepages()
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from agno.tools import Toolkit, tool

from utils.settings import settings

# ── 常量 ──────────────────────────────────────────────────
BEIJING_TZ = timezone(timedelta(hours=8))
DEFAULT_OUTPUT_DIR = Path(__file__).parent.parent / "output" / "media_monitor"
TRUNCATE_LIMIT = 8000
BRIGHTDATA_ENDPOINT = "https://api.brightdata.com/request"

# ── 23 家媒体源配置 ───────────────────────────────────────
MEDIA_SOURCES: List[Dict[str, str]] = [
    # ── 综合新闻 ──
    {
        "name": "BBC", "url": "https://www.bbc.com/news",
        "category": "综合新闻",
        "zone": "左侧3条 + 中间头条，共约4条",
        "layout": "中间大头条=level1, 左侧带头图2条=level2, 左侧其余1-2条=level3",
    },
    {
        "name": "CNN", "url": "https://edition.cnn.com",
        "category": "综合新闻",
        "zone": "左上方 + 中间头条 + 左中方，共约4条",
        "layout": "中间头条=level1, 左上带头图=level2, 左中2条无头图+分析/深度类=level3",
    },
    {
        "name": "Reuters", "url": "https://www.reuters.com",
        "category": "综合新闻",
        "zone": "左侧头条 + 右侧4条，共约5条",
        "layout": "左侧头条=level1, 右侧4条=level2",
    },
    {
        "name": "NYT", "url": "https://www.nytimes.com/international/",
        "category": "综合新闻",
        "zone": "首屏可见新闻，约6-8条",
        "layout": "顶部大头条(最大标题+大图)=level1, 其下2-3条次级标题=level2, 其余=level3",
    },
    {
        "name": "The Guardian", "url": "https://www.theguardian.com/international",
        "category": "综合新闻",
        "zone": "左侧头条区 + 中间头条，共约5-7条",
        "layout": "最顶部/最大标题=level1, 左侧次要头条2-3条=level2, 其余=level3",
    },
    # ── 中东 ──
    {
        "name": "Al Jazeera", "url": "https://www.aljazeera.com/",
        "category": "中东视角",
        "zone": "首屏可见新闻，约5-7条",
        "layout": "顶部大头条(最大标题)=level1, 其下2-3条=level2, 其余=level3",
    },
    # ── 美国政治 ──
    {
        "name": "Politico", "url": "https://www.politico.com/",
        "category": "美国政治",
        "zone": "首屏可见新闻，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条核心政治新闻=level2, 其余=level3。重点关注国会/白宫/选举类报道",
    },
    {
        "name": "The Hill", "url": "https://thehill.com/",
        "category": "美国政治",
        "zone": "首屏可见新闻，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条=level2, 其余=level3。重点关注国会动态/政策辩论",
    },
    # ── 财经商业 ──
    {
        "name": "Bloomberg", "url": "https://www.bloomberg.com/asia",
        "category": "财经商业",
        "zone": "首屏可见新闻，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条市场/经济核心报道=level2, 其余=level3",
    },
    {
        "name": "WSJ", "url": "https://www.wsj.com/",
        "category": "财经商业",
        "zone": "首屏可见新闻，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条=level2, 其余=level3",
    },
    {
        "name": "FT", "url": "https://www.ft.com/",
        "category": "财经商业",
        "zone": "首屏可见新闻，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条=level2, 其余=level3",
    },
    # ── 外交/国际关系 ──
    {
        "name": "Foreign Policy", "url": "https://foreignpolicy.com/",
        "category": "外交/国际关系",
        "zone": "首屏可见文章，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条核心分析=level2, 其余专栏/Report=level3。重点提取外交政策分析和专家署名文章",
    },
    {
        "name": "Foreign Affairs", "url": "https://www.foreignaffairs.com/",
        "category": "外交/国际关系",
        "zone": "首屏可见文章，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条=level2, 其余=level3。重点提取战略分析和学术观点",
    },
    {
        "name": "The Diplomat", "url": "https://thediplomat.com/",
        "category": "外交/国际关系",
        "zone": "首屏可见文章，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条=level2, 其余=level3。重点关注亚太地区外交/安全分析",
    },
    # ── 深度/杂志 ──
    {
        "name": "The Economist", "url": "https://www.economist.com/",
        "category": "深度/杂志",
        "zone": "首屏可见文章，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条核心分析=level2, 其余=level3。重点关注社论立场和分析性报道",
    },
    {
        "name": "The Atlantic", "url": "https://www.theatlantic.com/world/",
        "category": "深度/杂志",
        "zone": "首屏可见文章，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条=level2, 其余=level3。关注长篇分析和观点文章",
    },
    # ── 研究/智库 ──
    {
        "name": "Pew Research", "url": "https://www.pewresearch.org/",
        "category": "研究/智库",
        "zone": "首屏可见报告/文章，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条核心研究报告=level2, 其余=level3。重点关注数据和调查研究发现",
    },
    {
        "name": "Lowy Institute", "url": "https://www.lowyinstitute.org/",
        "category": "研究/智库",
        "zone": "首屏可见文章，约5-7条",
        "layout": "顶部大头条=level1, 其下2-3条=level2, 其余=level3。重点关注亚太政策分析和地缘战略研究",
    },
    # ── 中国专区 ──
    {
        "name": "CNN China", "url": "https://edition.cnn.com/world/china",
        "category": "中国专区",
        "zone": "首屏可见中国相关新闻，约6-8条",
        "layout": "顶部大头条=level1, 其下2-3条=level2, 其余=level3。重点关注涉华报道角度和叙事框架",
    },
    {
        "name": "BBC Chinese", "url": "https://www.bbc.com/zhongwen/simp",
        "category": "中国专区",
        "zone": "首页可见新闻，约6-8条",
        "layout": "顶部头条=level1, 其下2-3条=level2, 其余=level3。BBC中文版面向华语受众，留意其选题和叙事角度",
    },
    {
        "name": "Reuters China", "url": "https://www.reuters.com/world/china",
        "category": "中国专区",
        "zone": "首屏可见中国相关新闻，约6-8条",
        "layout": "顶部头条=level1, 其下2-3条=level2, 其余=level3。路透涉华报道以事实描述为主",
    },
    {
        "name": "WSJ China", "url": "https://www.wsj.com/world/china",
        "category": "中国专区",
        "zone": "首屏可见中国相关新闻，约6-8条",
        "layout": "顶部头条=level1, 其下2-3条=level2, 其余=level3。可能受付费墙限制",
    },
    {
        "name": "FT China", "url": "https://www.ft.com/china",
        "category": "中国专区",
        "zone": "首屏可见中国相关新闻，约6-8条",
        "layout": "顶部头条=level1, 其下2-3条=level2, 其余=level3。金融时报涉华财经视角，可能受付费墙限制",
    },
]


# ═══════════════════════════════════════════════════════════
# MediaMonitorToolkit（纯抓取层）
# ═══════════════════════════════════════════════════════════

class MediaMonitorToolkit(Toolkit):
    """媒体首页抓取工具：通过 BrightData API 抓取 23 家国际主流媒体首页。

    输出原始 Markdown 到磁盘，供后续 LLM Agent 分析使用。

    Args:
        brightdata_key:  BrightData API 密钥（默认从 config.settings 读取，env: BRIGHTDATA_API_KEY）
        brightdata_zone:  BrightData zone 名称（默认从 config.settings 读取，env: BRIGHTDATA_ZONE）
        output_dir:       输出目录（默认项目根/output/media_monitor）
    """

    def __init__(
        self,
        brightdata_key: Optional[str] = None,
        brightdata_zone: Optional[str] = None,
        output_dir: Optional[str] = None,
    ):
        super().__init__(name="media_monitor")

        self.brightdata_key = brightdata_key or settings.api_key
        self.brightdata_zone = brightdata_zone or settings.zone
        self.output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR

        self.register(self.scrape_all_homepages)
        self.register(self.scrape_single)

    # ── 底层 API 调用 ──────────────────────────────────────

    def _scrape_one_url(self, url: str, timeout: int = 60) -> Tuple[str, Optional[str]]:
        """调用 BrightData API 抓取单个 URL 的 Markdown。

        Returns:
            (status, content_or_error): status 为 "ok" 或 "error"
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.brightdata_key}",
        }
        payload = {
            "url": url,
            "zone": self.brightdata_zone,
            "format": "raw",
            "data_format": "markdown",
        }
        try:
            resp = requests.post(BRIGHTDATA_ENDPOINT, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 200:
                return "ok", resp.text
            else:
                return "error", f"HTTP {resp.status_code}: {resp.text[:300]}"
        except requests.RequestException as e:
            return "error", str(e)

    def _scrape_all_impl(
        self,
        raw_dir: Path,
        retries: int = 2,
        delay: float = 1.0,
    ) -> dict:
        """逐家爬取 23 家媒体首页，每条结果立即写入磁盘。"""
        raw_dir.mkdir(parents=True, exist_ok=True)
        results: dict = {}

        for i, src in enumerate(MEDIA_SOURCES, 1):
            name, url = src["name"], src["url"]
            print(f"[{i:02d}/{len(MEDIA_SOURCES)}] {name}: {url}  ...", end=" ", flush=True)

            last_err = None
            for attempt in range(1 + retries):
                if attempt > 0:
                    print(f"\n      ↩ 重试 {attempt}/{retries}...", end=" ", flush=True)
                    time.sleep(delay * (2 ** attempt))
                try:
                    status, content = self._scrape_one_url(url)
                    if status == "ok" and content and len(content) > 100:
                        safe_name = name.lower().replace(" ", "_")
                        raw_file = raw_dir / f"{safe_name}.md"
                        raw_file.write_text(content, encoding="utf-8")
                        results[name] = {"status": "ok", "file": str(raw_file), "len": len(content)}
                        print(f"✓ {len(content)} chars")
                        break
                    else:
                        last_err = f"内容太短或空 ({len(content) if content else 0} chars)"
                except Exception as e:
                    last_err = str(e)

            if name not in results:
                results[name] = {"status": "error", "error": last_err or "未知错误"}
                print(f"✗ {last_err}")

            if i < len(MEDIA_SOURCES):
                time.sleep(delay)

        ok_count = sum(1 for r in results.values() if r["status"] == "ok")
        print(f"\n📊 爬取完成: {ok_count}/{len(MEDIA_SOURCES)} 成功")
        return results

    # ── 注册为 Agent 工具的方法 ─────────────────────────────

    @tool(show_result=True)
    def scrape_all_homepages(
        self,
        output_dir: Optional[str] = None,
        retries: int = 2,
        delay: float = 1.0,
    ) -> Dict[str, Any]:
        """抓取 23 家国际主流媒体首页首屏，保存为 Markdown 文件。

        将原始 Markdown 保存到 raw/ 子目录，并返回抓取清单。

        Args:
            output_dir: 输出目录，默认使用初始化时指定的目录
            retries:    失败重试次数
            delay:      请求间隔秒数

        Returns:
            {"ok": N, "total": 23, "results": {媒体名: {"status": "ok"/"error", ...}}, "raw_dir": "...", "manifest_file": "..."}
        """
        base = Path(output_dir) if output_dir else self.output_dir
        raw_dir = base / "raw"
        results = self._scrape_all_impl(raw_dir, retries=retries, delay=delay)

        manifest = {
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "results": results,
        }
        manifest_file = base / "scrape_manifest.json"
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        ok_count = sum(1 for r in results.values() if r["status"] == "ok")
        return {
            "ok": ok_count,
            "total": len(MEDIA_SOURCES),
            "results": results,
            "raw_dir": str(raw_dir),
            "manifest_file": str(manifest_file),
        }

    @tool(show_result=True)
    def scrape_single(self, media_name: str) -> Dict[str, Any]:
        """抓取单个指定媒体的首页。

        Args:
            media_name: 媒体名称，如 "BBC"、"CNN" 等（不区分大小写）

        Returns:
            {"status": "ok", "name": "BBC", "content": "...", "len": N} 或 {"error": "..."}
        """
        src = next(
            (s for s in MEDIA_SOURCES if s["name"].lower() == media_name.lower()),
            None
        )
        if not src:
            return {"error": f"未找到媒体: {media_name}，可用: {[s['name'] for s in MEDIA_SOURCES]}"}

        try:
            status, content = self._scrape_one_url(src["url"])
            if status == "ok" and content and len(content) > 100:
                return {"status": "ok", "name": src["name"], "url": src["url"], "content": content, "len": len(content)}
            else:
                return {"error": f"抓取失败: {content}"}
        except Exception as e:
            return {"error": str(e)}


# ═══════════════════════════════════════════════════════════
# 命令行入口（仅测试抓取功能）
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    """直接运行时测试抓取功能。

    用法:
        python -m tools.media_monitor
        python -m tools.media_monitor BBC      # 抓取单个媒体
    """
    import sys

    scraper = MediaMonitorToolkit()

    if len(sys.argv) > 1:
        # 抓取单个媒体 — 使用内部实现
        src = next(
            (s for s in MEDIA_SOURCES if s["name"].lower() == sys.argv[1].lower()),
            None
        )
        if not src:
            print(f"未找到媒体: {sys.argv[1]}")
            print(f"可用: {[s['name'] for s in MEDIA_SOURCES]}")
            sys.exit(1)
        status, content = scraper._scrape_one_url(src["url"])
        if status == "ok" and content and len(content) > 100:
            print(f"✓ {src['name']}: {len(content)} chars")
            print(content[:500])
        else:
            print(f"✗ 抓取失败: {content}")
    else:
        # 批量抓取全部 — 使用内部实现
        if not scraper.brightdata_key:
            print("❌ BrightData API key 未设置（请设置 BRIGHTDATA_API_KEY 环境变量）")
            sys.exit(1)
        raw_dir = scraper.output_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        results = scraper._scrape_all_impl(raw_dir)
        manifest = {
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "results": results,
        }
        manifest_file = scraper.output_dir / "scrape_manifest.json"
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        ok_count = sum(1 for r in results.values() if r["status"] == "ok")
        print(f"\n✅ 完成，共 {ok_count}/{len(MEDIA_SOURCES)} 成功")
        print(f"📁 {raw_dir}")
        print(f"📋 {manifest_file}")
