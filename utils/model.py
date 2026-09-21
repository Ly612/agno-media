from agno.models.openai.like import OpenAILike

from utils.settings import AI_API_KEY, AI_BASE_URL, AI_MODEL_ID


def get_model() -> OpenAILike:
    return OpenAILike(
        id=AI_MODEL_ID,
        base_url=AI_BASE_URL,
        api_key=AI_API_KEY,
    )
