"""信源网站首页截图工具：用 Playwright 抓取各媒体首页首屏截图。

纯截图层，不含 LLM 逻辑。逐站点打开首页、按站点删除广告 / cookie 弹窗后
截取首屏（1280×720，16:9），保存 PNG。供 agents.media_monitor_agent 在日报末尾
追加「信源网站首页截图」板块使用。

浏览器优先用 AdsPower 指纹浏览器（各站点带自己的代理/指纹，翻墙更稳），通过
本地 API 启动固定 profile 后用 Playwright connect_over_cdp 接管；AdsPower 不可用
（未开客户端 / user_id 无效 / 未启用）时自动回退到本地 Playwright 自带 Chromium。

依赖:
    - playwright（需 `playwright install chromium` 安装浏览器二进制，兜底路径用）
    - AdsPower 客户端 + config.adspower_settings（.env 的 MEDIA_MONITOR_ADSPOWER_*）

用法:
    from utils.site_screenshot import capture_all

    shots = capture_all(Path("output/media_monitor/screenshots"))

    # 命令行:
    python -m tools.site_screenshot            # 优先 AdsPower，失败回退本地
    python -m tools.site_screenshot --headful  # 有头模式
    python -m tools.site_screenshot --local    # 强制本地 Chromium（跳过 AdsPower）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

# ── 常量 ──────────────────────────────────────────────────
# 首屏截图尺寸取 16:9（主流电脑显示器比例），观感更接近日常电脑屏幕。
# 截的是首屏（page.screenshot() 非整页），viewport 高度即决定成图比例。
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 720

# 截图站点列表（首页 URL，与各站点专属去广告选择器一一对应）。
# 注意：此处 URL 为各媒体首页（如 bbc.com/），与 tools.media_monitor.MEDIA_SOURCES
# 里的栏目页 URL（如 bbc.com/news）不同——截图取整站首页版面更直观。
SCREENSHOT_SITES: List[str] = [
    "https://edition.cnn.com/",
    "https://www.bbc.com/",
    "https://www.aljazeera.com/",
    "https://www.theguardian.com/international",
    "https://www.politico.com/",
    "https://www.bloomberg.com/asia",
    "https://foreignpolicy.com/",
    "https://www.nytimes.com/",
    "https://www.economist.com/",
    "https://thediplomat.com/",
    "https://www.foreignaffairs.com/",
    "https://www.wsj.com/",
    "https://thehill.com/",
    "https://www.ft.com/",
    "https://www.theatlantic.com/world/",
    "https://www.pewresearch.org/",
    "https://www.lowyinstitute.org/",
]


def _find_chromium_executable() -> Optional[str]:
    """探测一个已存在且完整的 Chromium 可执行文件路径。

    playwright 的浏览器版本与库版本强绑定，精确 build 常需联网下载；
    但缓存里往往已有其他完整 build（mac12 上新版 playwright 反而下不动）。
    这里优先用环境变量 PLAYWRIGHT_CHROMIUM_EXECUTABLE，否则在
    ms-playwright 缓存目录里挑一个**存在真实可执行文件**的 chromium build
    （偏好较新的版本号），交给 launch(executable_path=...) 使用。

    返回 None 表示未找到，此时回退到 playwright 默认查找逻辑
    （即用库自带版本匹配的浏览器）。
    """
    env = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if env and Path(env).exists():
        return env

    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    if not cache.exists():
        # Linux 缓存目录兜底
        cache = Path.home() / ".cache" / "ms-playwright"
    if not cache.exists():
        return None

    candidates: List[tuple] = []
    for d in cache.glob("chromium-*"):
        # 只取完整的 Chromium.app（headless_shell 目录不算完整浏览器）
        mac = d / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
        linux = d / "chrome-linux" / "chrome"
        exe = mac if mac.exists() else (linux if linux.exists() else None)
        if exe:
            # 按 build 号数字排序，偏好较新
            try:
                build = int(d.name.split("-")[-1])
            except ValueError:
                build = 0
            candidates.append((build, str(exe)))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _url_to_filename(url: str) -> str:
    """把 URL 转成安全的文件名，如 https://www.bbc.com/ → bbc_com.png。"""
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "").replace(".", "_")
    return f"{domain}.png"


def capture_site(page, url: str, output_dir: Path) -> Path:
    """打开单个站点首页，删除广告 / cookie 弹窗后截取首屏并保存。

    Args:
        page:       Playwright page 实例
        url:        待截图的站点首页 URL
        output_dir: 截图输出目录

    Returns:
        保存的 PNG 路径
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / _url_to_filename(url)

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)

    # ── 各站点专属：删除广告 / cookie 弹窗 / 订阅浮层，保证首屏干净 ──
    if url == "https://www.aljazeera.com/":
        # 删除 google ads
        page.eval_on_selector_all(
            "div.container--ads.container--ads-leaderboard-atf",
            "els => els.forEach(e => e.remove())"
        )
        # 删除 cookies
        page.eval_on_selector_all(
            "div#onetrust-consent-sdk",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://www.bbc.com/":
        # 删除 google ads
        page.eval_on_selector_all(
            '[data-testid="ad-unit"]',
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://edition.cnn.com/":
        # 删除 google ads
        page.eval_on_selector_all(
            "div.ad-slot-header__wrapper",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://www.theguardian.com/international":
        # 删除 google ads
        page.eval_on_selector_all(
            "div.dcr-swnnv4",
            "els => els.forEach(e => e.remove())"
        )
        # 删除 cookies
        page.eval_on_selector_all(
            "div#sp_message_container_1482255",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://www.politico.com/":
        # 删除空白区域
        page.eval_on_selector_all(
            "div#banner-wrap",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://www.bloomberg.com/asia":
        # 删除 google ads
        page.eval_on_selector_all(
            "div.media-ui-FullWidthAd_fullWidthAdWrapper-fClHZteIk3k-",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://foreignpolicy.com/":
        # 删除 google ads
        page.eval_on_selector_all(
            "div.home-ad-region-1",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://www.nytimes.com/":
        # 删除 google ads
        page.eval_on_selector_all(
            "div.css-v2w0j8.e1xxpj0j0",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://www.economist.com/":
        # 删除 iframe
        page.eval_on_selector_all(
            "iframe#sp_message_iframe_1491397",
            "els => els.forEach(e => e.remove())"
        )
        # 删除 google ads
        page.eval_on_selector_all(
            "div.css-9rhqok.e1fyg7r20",
            "els => els.forEach(e => e.remove())"
        )
        # 删除 subscribe
        page.eval_on_selector_all(
            "div#base-banner",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://thediplomat.com/":
        # 删除 subscribe
        page.eval_on_selector_all(
            "div.tda-modal-window",
            "els => els.forEach(e => e.remove())"
        )
        # 删除 google ads
        page.eval_on_selector_all(
            "section.td-ad-container--labeled",
            "els => els.forEach(e => e.remove())"
        )
        # 删除 newsletter
        page.eval_on_selector_all(
            "section#td-newsletter-home",
            "els => els.forEach(e => e.remove())"
        )
        # 删除 google ads
        page.eval_on_selector_all(
            "aside.td-ad-container--labeled",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://www.foreignaffairs.com/":
        # 删除 subscribe
        page.eval_on_selector_all(
            "div.position-fixed.bottom-0.w-100.z-above-base",
            "els => els.forEach(e => e.remove())"
        )
        # 删除 google ads
        page.eval_on_selector_all(
            "div.text-align-center.pt-15.c-bg-secondary.d-flex.dfp-tag-wrapper.justify-around",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://www.wsj.com/":
        pass
    elif url == "https://thehill.com/":
        pass
    elif url == "https://www.ft.com/":
        # 删除 cookies
        page.eval_on_selector_all(
            "iframe#sp_message_iframe_1503652",
            "els => els.forEach(e => e.remove())"
        )
        # 删除 google ads
        page.eval_on_selector_all(
            "div.slot-wrapper",
            "els => els.forEach(e => e.remove())"
        )
        # 删除弹窗
        page.eval_on_selector_all(
            "div.o-banner__outer",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://www.theatlantic.com/world/":
        pass
    elif url == "https://www.pewresearch.org/":
        # 删除 cookies
        page.eval_on_selector_all(
            "div.prc-privacy-consent__container",
            "els => els.forEach(e => e.remove())"
        )
    elif url == "https://www.lowyinstitute.org/":
        # 删除 notice
        page.eval_on_selector_all(
            '[aria-label="Privacy notice"]',
            "els => els.forEach(e => e.remove())"
        )

    shot = page.screenshot()
    output_file.write_bytes(shot)
    print(f"   ✓ [{url}] 截图已保存 → {output_file}")
    return output_file


# 反自动化检测的 init 脚本（AdsPower 与本地 Chromium 两条路径共用；对 AdsPower 无害）。
_ANTI_DETECT_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {} };
"""


def _shoot_sites(context, site_list: List[str], output_dir: Path) -> Dict[str, Path]:
    """在给定 Playwright context 上逐站点截图，单站点失败跳过。两条路径共用。"""
    shots: Dict[str, Path] = {}
    for i, url in enumerate(site_list, 1):
        print(f"   [{i:02d}/{len(site_list)}] {url} ...", flush=True)
        page = context.new_page()
        try:
            # AdsPower 窗口默认尺寸不定，显式设 viewport 保证 16:9 成图
            page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
            path = capture_site(page, url, output_dir)
            shots[url] = path
        except Exception as e:
            print(f"   ✗ [{url}] 截图失败，跳过: {e}")
        finally:
            page.close()
    return shots


def _capture_all_via_adspower(
    output_dir: Path,
    headless: bool,
    site_list: List[str],
) -> Dict[str, Path]:
    """用 AdsPower 指纹浏览器截图：启动固定 profile → connect_over_cdp → 逐站点截图。

    任何不可用（未启用 / 无 user_id / 客户端未开 / 启动失败）都抛异常，
    由 capture_all 捕获后回退本地 Chromium。
    """
    from playwright.sync_api import sync_playwright

    from utils.settings import adspower_settings
    from utils.adspower import AdsPowerClient

    if not adspower_settings.enabled:
        raise RuntimeError("AdsPower 未启用（MEDIA_MONITOR_ADSPOWER_ENABLED=false）")
    if not adspower_settings.user_id:
        raise RuntimeError("AdsPower user_id 未配置（MEDIA_MONITOR_ADSPOWER_USER_ID）")

    client = AdsPowerClient()
    if not client.is_up():
        raise RuntimeError("AdsPower 本地 API 不可用（未开客户端 / 端口不对）")

    ws = client.start(headless=headless)
    print(f"   ℹ 使用 AdsPower 指纹浏览器截图（profile={client.user_id}）")
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            # AdsPower 启动后已带一个默认 context，复用它（无则新建）
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            context.add_init_script(_ANTI_DETECT_INIT_SCRIPT)
            shots = _shoot_sites(context, site_list, output_dir)
            # 不 browser.close()：那会连带关掉用户的指纹浏览器进程；断开 CDP 即可
    finally:
        # 用正规 API 关闭 profile（best-effort）
        client.stop()

    print(f"📸 截图完成（AdsPower）: {len(shots)}/{len(site_list)} 成功")
    return shots


def _capture_all_via_local(
    output_dir: Path,
    headless: bool,
    site_list: List[str],
) -> Dict[str, Path]:
    """本地 Playwright 自带 Chromium 截图（AdsPower 不可用时的兜底路径）。"""
    from playwright.sync_api import sync_playwright, ViewportSize

    launch_kwargs: Dict[str, object] = {
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    # 精确 build 常需联网下载（mac12 上新版还下不动），优先复用缓存里已有的完整浏览器
    exe = _find_chromium_executable()
    if exe:
        launch_kwargs["executable_path"] = exe
        print(f"   ℹ 使用已有 Chromium: {exe}")

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport=ViewportSize(width=VIEWPORT_WIDTH, height=VIEWPORT_HEIGHT),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        context.add_init_script(_ANTI_DETECT_INIT_SCRIPT)
        shots = _shoot_sites(context, site_list, output_dir)
        context.close()
        browser.close()

    print(f"📸 截图完成（本地）: {len(shots)}/{len(site_list)} 成功")
    return shots


def capture_all(
    output_dir: Path,
    headless: bool = True,
    sites: Optional[List[str]] = None,
    use_adspower: bool = True,
) -> Dict[str, Path]:
    """逐站点截取首页首屏，单站点失败跳过、继续。

    优先用 AdsPower 指纹浏览器（带各站点自己的代理/指纹），不可用时自动回退到
    本地 Playwright Chromium，保证截图板块不缺失。

    Args:
        output_dir:   截图输出目录
        headless:     是否无头模式（默认 True，可在服务器 / 定时任务下运行）
        sites:        站点 URL 列表（默认 SCREENSHOT_SITES）
        use_adspower: 是否尝试 AdsPower（默认 True；False 直接走本地 Chromium）

    Returns:
        {url: png_path}，仅含截图成功的站点
    """
    site_list = sites if sites is not None else SCREENSHOT_SITES
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if use_adspower:
        try:
            return _capture_all_via_adspower(output_dir, headless, site_list)
        except Exception as e:
            print(f"   ⚠ AdsPower 截图不可用，回退本地 Chromium: {e}")

    return _capture_all_via_local(output_dir, headless, site_list)


# ═══════════════════════════════════════════════════════════
# 命令行入口（仅测试截图功能）
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    """直接运行时测试截图功能。

    用法:
        python -m tools.site_screenshot            # 优先 AdsPower，失败回退本地
        python -m tools.site_screenshot --headful  # 有头模式
        python -m tools.site_screenshot --local    # 强制本地 Chromium
    """
    import sys

    default_dir = Path(__file__).parent.parent / "output" / "media_monitor" / "screenshots"
    headless = "--headful" not in sys.argv
    use_adspower = "--local" not in sys.argv

    print(f"📸 信源网站首页截图（{'无头' if headless else '有头'}模式，"
          f"{'AdsPower 优先' if use_adspower else '强制本地'}）")
    print(f"📁 输出目录: {default_dir}")
    shots = capture_all(default_dir, headless=headless, use_adspower=use_adspower)
    for url, path in shots.items():
        print(f"  {url} → {path}")
