from langchain_mcp_adapters.client import MultiServerMCPClient
from .sample import get_current_time

LOCAL_TOOLS: list = [get_current_time]


async def load_tools(settings) -> list:
    tools = [*LOCAL_TOOLS]
    if settings.mcp_servers:
        client = MultiServerMCPClient(settings.mcp_servers)
        tools += await client.get_tools()
    return tools
