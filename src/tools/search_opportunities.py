import sqlite3
from contextlib import closing
from typing import Optional
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from src.settings import Settings

settings = Settings()
MAX_RESULTS = 50

RESULT_COLUMNS = ("notice_id", "title", "type", "posted_date", "response_deadline",
                  "naics_code", "type_of_set_aside_description", "pop_state", "ui_link")


class SearchOpportunitiesInput(BaseModel):
    keyword: Optional[str] = Field(
        default=None, description="Word or phrase to look for in the notice title or description text.")
    state: Optional[str] = Field(
        default=None, description="Place of performance as a full state name, for example 'texas'.")
    set_aside: Optional[str] = Field(
        default=None, description="Set-aside code to match exactly, for example 'SBA'.")
    posted_after: Optional[str] = Field(
        default=None, description="Only notices posted after this date as YYYY-MM-DD.")
    naics_code: Optional[str] = Field(
        default=None, description="Six-digit NAICS code to match exactly, for example '541512'.")
    limit: int = Field(
        default=25, description=f"Maximum number of results, 1 to {MAX_RESULTS}.")


def search_rows(connection, *, keyword=None, state=None, set_aside=None, posted_after=None, naics_code=None, limit=25):
    conditions = []
    parameters = {"limit": max(1, min(limit, MAX_RESULTS))}
    if keyword:
        conditions.append(
            "(title LIKE :keyword_pattern OR description_text LIKE :keyword_pattern)")
        parameters["keyword_pattern"] = f"%{keyword}%"
    if state:
        conditions.append("pop_state = :state COLLATE NOCASE")
        parameters["state"] = state
    if set_aside:
        conditions.append("type_of_set_aside = :set_aside COLLATE NOCASE")
        parameters["set_aside"] = set_aside
    if posted_after:
        conditions.append("posted_date > :posted_after")
        parameters["posted_after"] = posted_after
    if naics_code:
        conditions.append("naics_code = :naics_code")
        parameters["naics_code"] = naics_code
    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
    column_list = ", ".join(RESULT_COLUMNS)
    return connection.execute(
        f"""SELECT {column_list} FROM solicitations
        {where_clause}
        ORDER BY posted_date DESC
        LIMIT :limit""", parameters
    ).fetchall()


def format_rows(rows) -> str:
    if not rows:
        return "No matching opportunities found."
    blocks = []
    for row in rows:
        lines = [f"{column}: {value}" for column, value in zip(
            RESULT_COLUMNS, row) if value is not None]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@tool("search_opportunities", args_schema=SearchOpportunitiesInput)
def search_opportunities(keyword=None, state=None, set_aside=None, posted_after=None, naics_code=None, limit=25) -> str:
    """Search the local database of SAM.gov contract opportunities. All filters are optional and combined with AND. Returns summaries only; the description text is not included."""
    read_only_uri = settings.db_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(read_only_uri, uri=True)) as connection:
        rows = search_rows(connection, keyword=keyword, state=state, set_aside=set_aside,
                           posted_after=posted_after, naics_code=naics_code, limit=limit)
    return format_rows(rows)
