"""AdsPower 指纹浏览器本地 API 薄封装。

AdsPower 客户端在本机开放本地 API（默认 http://local.adspower.net:50325），
启动某个 profile（user_id）后返回一个 CDP ws 端点，外部可用 Playwright
connect_over_cdp 连上去操控这个带指纹/代理的浏览器。本模块只封装三个动作：

    - is_up():  探活（GET /status）
    - start():  启动 profile，返回 CDP ws 端点（GET /api/v1/browser/start）
    - stop():   关闭 profile（GET /api/v1/browser/stop）

只用标准库 urllib，无第三方依赖。**本地 API 走本机回环，必须直连、不走任何代理**
（截图时的翻墙由 AdsPower profile 自带的代理负责）。任何失败都抛可捕获的
RuntimeError，由调用方（tools.site_screenshot）决定是否回退本地 Chromium。

用法:
    from utils.adspower import AdsPowerClient

    client = AdsPowerClient()
    if client.is_up():
        ws = client.start()          # 用 config 里的固定 user_id
        # ... playwright.chromium.connect_over_cdp(ws) ...
        client.stop()

    # 命令行:
    python -m tools.adspower status
    python -m tools.adspower start [user_id]
    python -m tools.adspower stop  [user_id]
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from utils.settings import adspower_settings


class AdsPowerClient(object):
    """AdsPower 本地 API 客户端（status / browser.start / browser.stop）。

    Args:
        api_url: 本地 API 根地址（默认 config.adspower_settings.api_url）
        user_id: 固定 profile 的 user_id（默认 config.adspower_settings.user_id）
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        self.api_url = (api_url or adspower_settings.api_url).rstrip("/")
        self.user_id = user_id if user_id is not None else adspower_settings.user_id

    # ── 底层：本地 API GET（强制直连、不走代理）─────────────

    def _get(self, path: str, params: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        """对本地 API 发 GET，返回解析后的 JSON dict。

        用 build_opener(ProxyHandler({})) 显式禁用代理——本地回环地址绝不走代理，
        否则本机若设了 http_proxy 环境变量会把 127.0.0.1 也代理掉导致连不上。
        """
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.api_url}{path}"
        if query:
            url = f"{url}?{query}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"AdsPower 返回非 JSON: {raw[:200]}") from e

    # ── 公开：探活 ─────────────────────────────────────────

    def is_up(self, timeout: int = 5) -> bool:
        """GET /status，code==0 视为在线；任何异常返回 False（不抛出）。"""
        try:
            data = self._get("/status", {}, timeout=timeout)
            return data.get("code") == 0
        except Exception:
            return False

    # ── 公开：启动 profile，拿 CDP 端点 ────────────────────

    def start(
        self,
        user_id: Optional[str] = None,
        headless: bool = True,
        timeout: int = 60,
    ) -> str:
        """启动 profile，返回可供 Playwright connect_over_cdp 的 ws 端点。

        Args:
            user_id:  profile id（默认构造时的 self.user_id）
            headless: 是否无头启动（AdsPower headless=1）
            timeout:  请求超时秒数（启动浏览器较慢，默认 60）

        Returns:
            CDP ws 端点，如 ws://127.0.0.1:PORT/devtools/browser/<id>

        Raises:
            RuntimeError: user_id 缺失 / code!=0 / 响应缺少 ws 端点
        """
        uid = user_id if user_id is not None else self.user_id
        if not uid:
            raise RuntimeError("AdsPower user_id 未配置（MEDIA_MONITOR_ADSPOWER_USER_ID）")

        data = self._get(
            "/api/v1/browser/start",
            {"user_id": uid, "headless": 1 if headless else 0},
            timeout=timeout,
        )
        if data.get("code") != 0:
            raise RuntimeError(f"AdsPower 启动失败: {data.get('msg') or data}")

        ws = (((data.get("data") or {}).get("ws")) or {}).get("puppeteer")
        if not ws:
            raise RuntimeError(f"AdsPower 未返回 CDP 端点: {data}")
        return ws

    # ── 公开：关闭 profile（best-effort）───────────────────

    def stop(self, user_id: Optional[str] = None, timeout: int = 15) -> bool:
        """关闭 profile。best-effort：失败仅打印、返回 False，不抛出。"""
        uid = user_id if user_id is not None else self.user_id
        if not uid:
            return False
        try:
            data = self._get("/api/v1/browser/stop", {"user_id": uid}, timeout=timeout)
            return data.get("code") == 0
        except Exception as e:
            print(f"   ⚠ AdsPower 关闭 profile 失败（忽略）: {e}")
            return False


if __name__ == "__main__":
    """命令行手测入口。

    用法:
        python -m tools.adspower status
        python -m tools.adspower start [user_id]
        python -m tools.adspower stop  [user_id]
    """
    import sys

    argv = sys.argv[1:]
    action = argv[0] if argv else "status"
    uid = argv[1] if len(argv) > 1 else None
    client = AdsPowerClient(user_id=uid) if uid else AdsPowerClient()

    if action == "status":
        print("✅ AdsPower 在线" if client.is_up() else "❌ AdsPower 不可用（未开客户端 / 端口不对）")
    elif action == "start":
        try:
            ws = client.start()
            print(f"✅ 已启动 profile，CDP 端点:\n   {ws}")
        except Exception as e:
            print(f"❌ 启动失败: {e}")
            sys.exit(1)
    elif action == "stop":
        print("✅ 已关闭 profile" if client.stop() else "⚠ 关闭失败或无 user_id")
    else:
        print("用法: python -m tools.adspower status|start|stop [user_id]")
        sys.exit(1)
