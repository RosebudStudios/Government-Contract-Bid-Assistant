from langchain_mcp_adapters.client import MultiServerMCPClient
from .search_opportunities import search_opportunities
from .get_opportunity import get_opportunity

LOCAL_TOOLS: list = [search_opportunities, get_opportunity]


async def load_tools(settings) -> list:
    tools = [*LOCAL_TOOLS]
    if settings.mcp_servers:
        client = MultiServerMCPClient(settings.mcp_servers)
        tools += await client.get_tools()
    return tools
