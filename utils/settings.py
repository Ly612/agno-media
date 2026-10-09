import os
from pathlib import Path

from dotenv import load_dotenv

from utils.config import load_config

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

_config = load_config()

_ai = _config.get("ai", {})
AI_BASE_URL = _ai.get("base_url", "https://gate.aidingqing.com/v1")
AI_MODEL_ID = _ai.get("model_id", "q-GPT-5.6-Sol-Standard")
AI_API_KEY = os.getenv("AI_API_KEY", "")

# SerpAPI（Google 搜索/新闻）密钥，从 .env 读取
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")

# SocialCrawl（Facebook 等社媒抓取）密钥与基础地址
SOCIALCRAWL_API_KEY = os.getenv("SOCIALCRAWL_API_KEY", "")
SOCIALCRAWL_BASE_URL = _config.get("socialcrawl", {}).get(
    "base_url", "https://www.socialcrawl.dev"
).rstrip("/")

_db = _config.get("db", {})
DB_FILE = str(BASE_DIR / _db.get("file", "agno_media.db"))

_server = _config.get("server", {})
SERVER_HOST = _server.get("host", "localhost")
SERVER_PORT = _server.get("port", 7777)
SERVER_RELOAD = _server.get("reload", True)
SERVER_TRACING = _server.get("tracing", True)

# 导出报告的存放目录与对外下载地址
EXPORT_DIR = BASE_DIR / "exports"
# 对外访问地址：优先用 config 的 public_base_url，否则回退到 host:port（本地开发）
_public = (_server.get("public_base_url") or "").rstrip("/")
_base = _public if _public else f"http://{SERVER_HOST}:{SERVER_PORT}"
DOWNLOAD_BASE_URL = f"{_base}/download"

# 允许跨域访问的前端来源列表。config 留空则回退为 ["*"]（放行所有来源）
CORS_ORIGINS = _server.get("cors_origins") or ["*"]

# MCP Server（供豆包等 MCP Client 接入）监听配置
_mcp = _config.get("mcp", {})
MCP_HOST = _mcp.get("host", "0.0.0.0")
MCP_PORT = _mcp.get("port", 8000)
# MCP 接入鉴权 token（从 .env 读取）。留空则不校验（不推荐对外暴露时留空）
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")


# ═══════════════════════════════════════════════════════════
# 媒体监控流水线（media_monitor）相关配置
#
# 敏感 token 全部从 .env 读取；非敏感默认值从 config.yaml 的
# media_monitor / scheduler 段读取。各外部服务无凭证时，对应阶段会
# 在流水线里自动跳过（源码已有兜底），不阻塞核心流程。
# ═══════════════════════════════════════════════════════════

_mm = _config.get("media_monitor", {})
_sched = _config.get("scheduler", {})

# 流水线输出目录（首页原文、话题、摘要、报告等中间产物都落在这里）
MEDIA_MONITOR_OUTPUT_DIR = BASE_DIR / "output" / "media_monitor"

# ── BrightData（首页抓取 + 正文抓取）────────────────────────
# env: BRIGHT_DATA_API_KEY / BRIGHT_DATA_ZONE
BRIGHTDATA_API_KEY = os.getenv("BRIGHT_DATA_API_KEY", "")
BRIGHTDATA_ZONE = os.getenv("BRIGHT_DATA_ZONE", "")

# ── 媒体监控专用 LLM 网关 ───────────────────────────────────
# 推理/thinking 模型会先耗输出 token 做思考，故 max_tokens 调大。
# key/base_url/model 缺省回落到目标项目已有的 AI_* 网关。
MM_LLM_API_KEY = os.getenv("MEDIA_MONITOR_LLM_API_KEY", "") or AI_API_KEY
MM_LLM_BASE_URL = (
    os.getenv("MEDIA_MONITOR_LLM_BASE_URL", "")
    or _mm.get("llm_base_url")
    or AI_BASE_URL
)
MM_LLM_MODEL = (
    os.getenv("MEDIA_MONITOR_LLM_MODEL", "")
    or _mm.get("llm_model")
    or AI_MODEL_ID
)
MM_LLM_MAX_TOKENS = int(_mm.get("llm_max_tokens", 16000))

# ── Apify（社交平台抓取：Facebook / X / Google News）─────────
APIFY_API_KEY = os.getenv("APIFY_API_KEY", "")
APIFY_PROXY = os.getenv("APIFY_PROXY", "") or "http://127.0.0.1:7897"

# ── 飞书自建应用机器人（推送 Word 日报）──────────────────────
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_CHAT_ID = os.getenv("FEISHU_CHAT_ID", "")

# ── 邮件（SMTP）发送 ────────────────────────────────────────
MM_SMTP_HOST = os.getenv("MEDIA_MONITOR_SMTP_HOST", "")
MM_SMTP_PORT = int(os.getenv("MEDIA_MONITOR_SMTP_PORT", "465"))
MM_SMTP_USER = os.getenv("MEDIA_MONITOR_SMTP_USER", "")
MM_SMTP_PASSWORD = os.getenv("MEDIA_MONITOR_SMTP_PASSWORD", "")
MM_SMTP_MAIL_FROM = os.getenv("MEDIA_MONITOR_SMTP_MAIL_FROM", "")
MM_SMTP_MAIL_TO = os.getenv("MEDIA_MONITOR_SMTP_MAIL_TO", "")
MM_SMTP_MAIL_BCC = os.getenv("MEDIA_MONITOR_SMTP_MAIL_BCC", "")
MM_SMTP_USE_SSL = os.getenv("MEDIA_MONITOR_SMTP_USE_SSL", "true").lower() not in (
    "false",
    "0",
    "no",
)

# ── AdsPower 指纹浏览器（截图阶段可选，用于带代理/指纹出图）────
ADSPOWER_API_URL = (
    os.getenv("MEDIA_MONITOR_ADSPOWER_API_URL", "")
    or "http://local.adspower.net:50325"
).rstrip("/")
ADSPOWER_USER_ID = os.getenv("MEDIA_MONITOR_ADSPOWER_USER_ID", "")
ADSPOWER_ENABLED = os.getenv("MEDIA_MONITOR_ADSPOWER_ENABLED", "true").lower() not in (
    "false",
    "0",
    "no",
)

# ── 定时任务（APScheduler，随 main.py 启动）──────────────────
SCHEDULER_ENABLED = bool(_sched.get("enabled", False))
SCHEDULER_CRON = (_sched.get("cron") or "").strip()
SCHEDULER_HOUR = _sched.get("hour", 8)
SCHEDULER_MINUTE = _sched.get("minute", 0)
SCHEDULER_DAY_OF_WEEK = str(_sched.get("day_of_week", "*"))
SCHEDULER_TIMEZONE = _sched.get("timezone", "Asia/Shanghai")

# 流水线默认行为（可被 Agent 工具参数覆盖）
MM_PUSH_FEISHU = bool(_mm.get("push_feishu", False))
MM_PUSH_EMAIL = bool(_mm.get("push_email", False))
MM_SKIP_SOCIAL = bool(_mm.get("skip_social", False))
MM_SKIP_SCREENSHOT = bool(_mm.get("skip_screenshot", False))
MM_SKIP_WORD = bool(_mm.get("skip_word", False))


# ── 轻量配置对象 ────────────────────────────────────────────
# 让从 qf-toolkit 移植过来的工具（apify_scraper / feishu_bot /
# email_sender / adspower / site_screenshot）几乎零改动：源代码里用
# `xxx_settings.<attr>` 访问配置，这里提供同形状的简单对象即可。


class _BrightDataConf:
    api_key = BRIGHTDATA_API_KEY
    zone = BRIGHTDATA_ZONE


class _ApifyConf:
    api_key = APIFY_API_KEY
    proxy = APIFY_PROXY


class _FeishuConf:
    app_id = FEISHU_APP_ID
    app_secret = FEISHU_APP_SECRET
    chat_id = FEISHU_CHAT_ID


class _AdsPowerConf:
    api_url = ADSPOWER_API_URL
    user_id = ADSPOWER_USER_ID
    enabled = ADSPOWER_ENABLED


class _EmailConf:
    host = MM_SMTP_HOST
    port = MM_SMTP_PORT
    user = MM_SMTP_USER
    password = MM_SMTP_PASSWORD
    mail_from = MM_SMTP_MAIL_FROM
    mail_to = MM_SMTP_MAIL_TO
    mail_bcc = MM_SMTP_MAIL_BCC
    use_ssl = MM_SMTP_USE_SSL

    @property
    def recipients(self) -> list[str]:
        return [a.strip() for a in self.mail_to.split(",") if a.strip()]

    @property
    def bcc_recipients(self) -> list[str]:
        return [a.strip() for a in self.mail_bcc.split(",") if a.strip()]

    @property
    def sender(self) -> str:
        return self.mail_from or self.user


class _MediaMonitorLLMConf:
    llm_api_key = MM_LLM_API_KEY
    llm_base_url = MM_LLM_BASE_URL
    llm_model = MM_LLM_MODEL
    llm_max_tokens = MM_LLM_MAX_TOKENS


# 供移植工具 import 的单例（名字沿用源项目习惯，方便对照）
settings = _BrightDataConf()
bright_data_settings = _BrightDataConf()
apify_settings = _ApifyConf()
feishu_settings = _FeishuConf()
adspower_settings = _AdsPowerConf()
email_settings = _EmailConf()
media_monitor_settings = _MediaMonitorLLMConf()
