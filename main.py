import importlib
import pkgutil
from pathlib import Path

from agno.agent import Agent
from agno.os import AgentOS

from utils.db import get_db
from utils.settings import SERVER_HOST, SERVER_PORT, SERVER_RELOAD, SERVER_TRACING

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


if __name__ == "__main__":
    agent_os.serve(
        app="main:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=SERVER_RELOAD,
    )
