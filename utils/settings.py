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

_db = _config.get("db", {})
DB_FILE = str(BASE_DIR / _db.get("file", "agno_media.db"))

_server = _config.get("server", {})
SERVER_HOST = _server.get("host", "localhost")
SERVER_PORT = _server.get("port", 7777)
SERVER_RELOAD = _server.get("reload", True)
SERVER_TRACING = _server.get("tracing", True)
