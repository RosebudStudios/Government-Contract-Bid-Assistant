from datetime import datetime, timezone
from langchain_core.tools import tool


@tool("get_current_time", description="Returns the current UTC date and time as an ISO string. Use when the user asks what time or date it is. Takes no input.")
def get_current_time() -> str:
    return datetime.now(timezone.utc).isoformat()
