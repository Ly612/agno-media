import importlib
import pkgutil
from pathlib import Path

from agno.agent import Agent
from agno.os import AgentOS
from fastapi import HTTPException
from fastapi.responses import FileResponse

from utils.db import get_db
from utils.settings import (
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


@app.get("/download/{filename}")
def download_report(filename: str):
    filepath = (EXPORT_DIR / filename).resolve()
    # 防止路径穿越，确保文件在导出目录内
    if EXPORT_DIR.resolve() not in filepath.parents or not filepath.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        path=filepath,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    agent_os.serve(
        app="main:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=SERVER_RELOAD,
    )
