from agno.agent import Agent

from utils.config import get_agent_config
from utils.db import get_db
from utils.model import get_model


def build_agent() -> Agent:
    cfg = get_agent_config("media_agent")
    return Agent(
        name=cfg.get("name", "Media Agent"),
        model=get_model(),
        db=get_db(),
        instructions=cfg.get("instructions", []),
        add_history_to_context=True,
        num_history_runs=cfg.get("num_history_runs", 3),
        markdown=True,
    )
