import httpx
import asyncio
import json
import sqlite3
from .settings import Settings, require_env
from datetime import date, timedelta
from pathlib import Path

settings = Settings()
BASE = settings.sam_base_url


def init_db(path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS solicitations(
    notice_id TEXT PRIMARY KEY NOT NULL, title TEXT, solicitation_number TEXT, type TEXT, posted_date TEXT, response_deadline TEXT, naics_code TEXT, classification_code TEXT,
    type_of_set_aside TEXT, type_of_set_aside_description TEXT, full_parent_path_name TEXT, ui_link TEXT, description TEXT, pop_city TEXT, pop_state TEXT,
    office_city TEXT, office_state TEXT, contact_name TEXT, contact_email TEXT, resource_links TEXT, active TEXT, archive_date TEXT
    )""")
    con.commit()
    return con


def upsert(con, records):
    if not records:
        return 0
    cols = list(records[0])
    column_list = ", ".join(cols)
    placeholders = ", ".join(":" + c for c in cols)
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in cols if c != "notice_id")
    sql = f"""INSERT INTO solicitations ({column_list}) VALUES ({placeholders}) ON CONFLICT(notice_id) DO UPDATE SET {updates}"""
    con.executemany(sql, records)
    con.commit()
    return len(records)


class SamClient:
    def __init__(self, api_key=None, client=None):
        self.api_key = api_key or require_env("SAM_API_KEY")
        self.client = client or httpx.AsyncClient(timeout=30)

    async def search(self, posted_from, posted_to, *, limit=1000, max_pages=None, **filters):
        offset = 0
        seen = 0
        while True:
            params = {"api_key": self.api_key, "limit": limit, "offset": offset,
                      "postedFrom": posted_from.strftime("%m/%d/%Y"),
                      "postedTo": posted_to.strftime("%m/%d/%Y"), **filters}
            try:
                request = await self.client.get(BASE, params=params)
                if request.status_code == 404:
                    return
                if request.status_code == 429:
                    raise RuntimeError(
                        f"SAM.gov rate limit hit (429). Body: {request.text[:200]}")
                request.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise RuntimeError(
                    f"SAM.gov error {e.response.status_code}") from None
            data = request.json()
            rows = data.get("opportunitiesData") or []
            for row in rows:
                yield row
            seen += len(rows)
            if not rows or seen >= data.get("totalRecords", 0):
                return
            offset += 1
            if max_pages is not None and offset >= max_pages:
                return


def normalize(row) -> dict:
    place = row.get("placeOfPerformance") or {}
    office = row.get("officeAddress") or {}
    contacts = row.get("pointOfContact") or []
    first = contacts[0] if contacts else {}
    links = row.get("resourceLinks")
    formatted_dict = {"notice_id": row.get("noticeId"), "title": row.get("title"),
                      "solicitation_number": row.get("solicitationNumber"), "type": row.get("type"),
                      "posted_date": row.get("postedDate"), "response_deadline": row.get("responseDeadLine"),
                      "naics_code": row.get("naicsCode"), "classification_code": row.get("classificationCode"),
                      "type_of_set_aside": row.get("typeOfSetAside"), "type_of_set_aside_description": row.get("typeOfSetAsideDescription"),
                      "full_parent_path_name": row.get("fullParentPathName"), "ui_link": row.get("uiLink"),
                      "description": row.get("description"),
                      "pop_city": (place.get("city") or {}).get("name"), "pop_state": (place.get("state") or {}).get("name"),
                      "office_city": office.get("city"), "office_state": office.get("state"),
                      "contact_name": first.get("fullName"), "contact_email": first.get("email"),
                      "resource_links": json.dumps(links) if links else None, "active": row.get("active"), "archive_date": row.get("archiveDate")
                      }
    EMPTY = {"", "none", "n/a"}
    for key, value in formatted_dict.items():
        if isinstance(value, str) and value.strip().lower() in EMPTY:
            formatted_dict[key] = None
    return formatted_dict


async def main():
    samclient = SamClient()
    today = date.today()
    records = []
    async for row in samclient.search(today - timedelta(days=1), today, limit=1000, max_pages=1):
        record = normalize(row)
        records.append(record)
    db = init_db(settings.db_path)
    try:
        row_count = upsert(db, records)
    finally:
        db.close()
    print(f"upserted {row_count} records into {settings.db_path}")

if __name__ == "__main__":
    asyncio.run(main())
