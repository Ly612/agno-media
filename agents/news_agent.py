from agno.agent import Agent

from utils.config import get_agent_config
from utils.db import get_db
from utils.model import get_model
from utils.serpapi_tools import google_news, google_search


def build_agent() -> Agent:
    cfg = get_agent_config("news_agent")
    return Agent(
        name=cfg.get("name", "News Agent"),
        model=get_model(),
        db=get_db(),
        tools=[google_news, google_search],
        instructions=cfg.get(
            "instructions",
            [
                "You help users find information via web search.",
                "If the question is about news, current events, or the latest updates, "
                "call the google_news tool.",
                "Otherwise, call the google_search tool.",
                "After getting results, first give a concise summary in the user's language, "
                "then present the results as a markdown table with columns: "
                "标题 | 来源 | 时间 | 链接.",
                "Always cite the source links in the table.",
            ],
        ),
        add_history_to_context=True,
        num_history_runs=cfg.get("num_history_runs", 3),
        markdown=True,
    )
