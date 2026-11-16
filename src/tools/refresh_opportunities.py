import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.pipeline import ingestion_pipeline


class RefreshOpportunitiesInput(BaseModel):
    days: int = Field(
        default=1, ge=1, le=7,
        description="How many days back to look for new notices. Use 1 unless the user asks for more.")
    max_pages: int | None = Field(
        default=None, ge=1, le=7,
        description="The maximum number of pages to search through on sam.gov. No limit to pages until the quota is met if left as None.")


@tool("refresh_opportunities", args_schema=RefreshOpportunitiesInput)
async def refresh_opportunities(days: int = 1, max_pages: int | None = None) -> str:
    """Pull newly posted notices (and their descriptions) from SAM.gov into the local database.
    Uses the limited daily SAM.gov request quota, so call it only when the user asks for fresh data."""
    try:
        return await ingestion_pipeline(days=days, max_pages=max_pages)
    except (RuntimeError, httpx.HTTPError) as refresh_error:
        return (f"The refresh failed ({type(refresh_error).__name__}). "
                "Check the SAM.gov connection and try again later.")
