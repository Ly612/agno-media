import importlib
import mimetypes
import pkgutil
from pathlib import Path

from agno.agent import Agent
from agno.os import AgentOS
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from utils.db import get_db
from utils.settings import (
    CORS_ORIGINS,
    EXPORT_DIR,
    SERVER_HOST,
    SERVER_PORT,
    SERVER_RELOAD,
    SERVER_TRACING,
)

AGENTS_DIR = Path(__file__).resolve().parent / "agents"


def load_agents() -> list[Agent]:
    agents: list[Agent] = []
    for module_info in pkgutil.iter_modules([str(AGENTS_DIR)]):
        name = module_info.name
        if not name.endswith("_agent"):
            continue
        module = importlib.import_module(f"agents.{name}")
        build = getattr(module, "build_agent", None)
        if callable(build):
            agents.append(build())
    return agents


agent_os = AgentOS(
    agents=load_agents(),
    db=get_db(),
    tracing=SERVER_TRACING,
)
app = agent_os.get_app()

# 允许前端（agent-ui）跨域访问后端 API
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/download/{filename}")
def download_report(filename: str):
    filepath = (EXPORT_DIR / filename).resolve()
    # 防止路径穿越，确保文件在导出目录内
    if EXPORT_DIR.resolve() not in filepath.parents or not filepath.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    # 按扩展名推断 MIME（.xlsx / .docx 等各自对应），未知则交给浏览器兜底
    media_type, _ = mimetypes.guess_type(filename)
    return FileResponse(
        path=filepath,
        filename=filename,
        media_type=media_type or "application/octet-stream",
    )


if __name__ == "__main__":
    # 启动进程内定时任务（media_monitor 日报）。
    # reload 模式下 uvicorn 会 fork 工作子进程，定时器只应在主进程注册一次：
    # 用 RUN_MAIN 环境变量区分（reload 的子进程里该变量为 "true"）。开发态
    # 默认 scheduler.enabled=false，不会误触发真实抓取。
    import os

    from utils.scheduler import start_scheduler

    if not SERVER_RELOAD or os.environ.get("RUN_MAIN") != "true":
        start_scheduler()

    agent_os.serve(
        app="main:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=SERVER_RELOAD,
    )
