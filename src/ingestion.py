import httpx
import json
import sqlite3
import argparse
import os
from dataclasses import dataclass
from email.message import Message
from .settings import Settings, require_env
from datetime import date, timedelta
from pathlib import Path

settings = Settings()
BASE = settings.sam_base_url
SAM_SYNC_NAME = "sam_opportunities"
SUPPORTED_EXTENSIONS = (".pdf", ".docx")


class RateLimitError(RuntimeError):  # command line options
    """SAM.gov answered 429: the daily request quota is gone."""


@dataclass(frozen=True)
class DownloadResult:
    status: str
    filename: str | None = None
    content_type: str | None = None
    saved_path: Path | None = None


def parse_filename(content_disposition):
    if not content_disposition:
        return None
    message = Message()
    message["content-disposition"] = content_disposition
    return message.get_filename()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Sync SAM.gov solicitations into the local database.")
    parser.add_argument("--days", type=int, default=1,
                        help="how many days back to search (1-364)")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="stop after this many pages (omit to fetch all)")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 364:
        parser.error("--days must be between 1 and 364")
    return args


def init_db(path) -> sqlite3.Connection:  # table creation
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE IF NOT EXISTS solicitations(
    notice_id TEXT PRIMARY KEY NOT NULL, title TEXT, solicitation_number TEXT, type TEXT, posted_date TEXT, response_deadline TEXT, naics_code TEXT, classification_code TEXT,
    type_of_set_aside TEXT, type_of_set_aside_description TEXT, full_parent_path_name TEXT, ui_link TEXT, description TEXT, pop_city TEXT, pop_state TEXT,
    office_city TEXT, office_state TEXT, contact_name TEXT, contact_email TEXT, resource_links TEXT, active TEXT, archive_date TEXT, description_text TEXT
    )""")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS sync_state(sync_name TEXT PRIMARY KEY NOT NULL, last_synced_date TEXT)""")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS api_usage(day TEXT PRIMARY KEY NOT NULL, request_count INTEGER NOT NULL)""")
    existing_column_names = [column_row[1] for column_row in connection.execute(
        "PRAGMA table_info(solicitations)")]
    if "description_text" not in existing_column_names:
        connection.execute(
            "ALTER TABLE solicitations ADD COLUMN description_text TEXT")
    connection.execute("""CREATE TABLE IF NOT EXISTS documents(
        id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id TEXT NOT NULL, link TEXT UNIQUE NOT NULL, filename TEXT,
        content_type TEXT, status TEXT NOT NULL DEFAULT 'pending', error TEXT)""")
    connection.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks USING fts5(
    chunk_text, document_id UNINDEXED, notice_id UNINDEXED, page_number UNINDEXED, tokenize = 'porter unicode61'
    )""")
    connection.commit()
    return connection


class SamClient:  # talking to sam.gov and fetching the raw data
    def __init__(self, api_key=None, client=None):
        self.api_key = api_key or require_env("SAM_API_KEY")
        self.client = client or httpx.AsyncClient(timeout=30)
        self.stopped_early = False
        self.requests_made = 0

    async def _get(self, url, params=None, *, accept_redirect=False, counts_toward_quota=True):
        if counts_toward_quota:
            self.requests_made += 1
        try:
            response = await self.client.get(url, params=params, follow_redirects=False)
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                raise RateLimitError(
                    f"SAM.gov rate limit hit (429). Body: {response.text[:200]}")
            if accept_redirect and response.is_redirect:
                return response
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as http_error:
            response_body = http_error.response.text[:200].replace(
                self.api_key, "***")
            raise RuntimeError(
                f"SAM.gov error {http_error.response.status_code}: {response_body}") from None

    async def aclose(self):
        await self.client.aclose()

    async def search(self, posted_from, posted_to, *, limit=1000, max_pages=None, **filters):
        offset = 0
        seen = 0
        self.stopped_early = False
        while True:
            params = {"api_key": self.api_key, "limit": limit, "offset": offset,
                      "postedFrom": posted_from.strftime("%m/%d/%Y"),
                      "postedTo": posted_to.strftime("%m/%d/%Y"), **filters}

            response = await self._get(BASE, params)
            if response is None:
                return
            page = response.json()
            rows = page.get("opportunitiesData") or []
            for row in rows:
                yield row
            seen += len(rows)
            if not rows or seen >= page.get("totalRecords", 0):
                return
            offset += 1
            if max_pages is not None and offset >= max_pages:
                self.stopped_early = True
                return

    async def fetch_description(self, description_url):
        url_with_key = httpx.URL(description_url).copy_merge_params(
            {"api_key": self.api_key})
        response = await self._get(url_with_key)
        if response is None:
            return ""
        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        description_text = payload.get("description")
        if not isinstance(description_text, str):
            return ""
        if not description_text.strip() or description_text.strip() == "Description Not Found":
            return ""
        return description_text

    async def download_attachment(self, link, folder, file_stem, *, max_bytes, counts_toward_quota=False):
        url_with_key = httpx.URL(link).copy_merge_params(
            {"api_key": self.api_key})
        redirect = await self._get(url_with_key, accept_redirect=True, counts_toward_quota=counts_toward_quota)
        if redirect is None:
            return DownloadResult("not_found")
        if not redirect.is_redirect:
            raise RuntimeError(
                f"SAM.gov answered {redirect.status_code}, expected a redirect")
        filename = parse_filename(redirect.headers.get("content-disposition"))
        content_type = redirect.headers.get("content-type")
        extension = Path(filename).suffix.lower() if filename else ""
        if extension not in SUPPORTED_EXTENSIONS:
            return DownloadResult("unsupported", filename, content_type)
        file_url = httpx.URL(redirect.headers.get("location", ""))
        if file_url.scheme != "https":
            raise RuntimeError("redirect target is not an https address")
        saved_path = Path(folder) / f"{file_stem}{extension}"
        part_path = saved_path.with_name(saved_path.name + ".part")
        Path(folder).mkdir(parents=True, exist_ok=True)
        try:
            async with self.client.stream("GET", file_url) as file_response:
                if file_response.status_code != 200:
                    raise RuntimeError(
                        f"file host answered {file_response.status_code}")
                if int(file_response.headers.get("content-length") or 0) > max_bytes:
                    return DownloadResult("too_large", filename, content_type)
                total_bytes = 0
                with open(part_path, "wb") as part_file:
                    async for chunk in file_response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > max_bytes:
                            return DownloadResult("too_large", filename, content_type)
                        part_file.write(chunk)
            os.replace(part_path, saved_path)
        except httpx.HTTPError:
            raise RuntimeError("file download failed") from None
        finally:
            part_path.unlink(missing_ok=True)
        return DownloadResult("downloaded", filename, content_type, saved_path)


def mark_day_exhausted(connection, day, daily_request_limit):
    connection.execute(
        """INSERT INTO api_usage (day, request_count) VALUES (:day, :daily_request_limit)
           ON CONFLICT(day) DO UPDATE SET request_count = max(request_count, :daily_request_limit)""",
        {"day": day, "daily_request_limit": daily_request_limit})
    connection.commit()


def normalize(row) -> dict:  # reshaping the data that comes back
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


def upsert(connection, records):  # storing the notices
    if not records:
        return 0
    column_names = list(records[0])
    column_list = ", ".join(column_names)
    placeholders = ", ".join(":" + column_name for column_name in column_names)
    updates = ", ".join(
        f"{column_name} = excluded.{column_name}" for column_name in column_names if column_name != "notice_id")
    sql = f"""INSERT INTO solicitations ({column_list}) VALUES ({placeholders}) ON CONFLICT(notice_id) DO UPDATE SET {updates}"""
    connection.executemany(sql, records)
    connection.commit()
    return len(records)


def get_last_synced_date(connection):  # bookmark, read
    row = connection.execute(
        """SELECT last_synced_date FROM sync_state WHERE sync_name = :sync_name""", {"sync_name": SAM_SYNC_NAME},).fetchone()
    if row is None:
        return None
    return date.fromisoformat(row[0])


def save_last_synced_date(connection, synced_date):  # bookmark, write
    connection.execute("""INSERT INTO sync_state(sync_name, last_synced_date)
      VALUES (:sync_name, :last_synced_date)
       ON CONFLICT(sync_name) DO UPDATE SET last_synced_date = excluded.last_synced_date""",
                       {"sync_name": SAM_SYNC_NAME, "last_synced_date": synced_date.isoformat()})
    connection.commit()
    return


# turns bookmark into a start date
def choose_posted_from(last_synced_date, today, days, lookback_days):
    if last_synced_date is None:
        wanted_start = today - timedelta(days=days)
    else:
        wanted_start = last_synced_date - timedelta(days=lookback_days)
    earliest_allowed = today - timedelta(days=364)
    return max(wanted_start, earliest_allowed)


def get_requests_used(connection, day):  # quota counter, read
    row = connection.execute(
        """SELECT request_count FROM api_usage WHERE day = :day""", {"day": day},).fetchone()
    if row is None:
        return 0
    return row[0]


def add_requests_used(connection, day, request_count):  # quota counter, write
    connection.execute(
        """INSERT INTO api_usage (day, request_count) VALUES (:day, :request_count) ON CONFLICT(day) DO UPDATE SET request_count = request_count+excluded.request_count""", {"day": day, "request_count": request_count})
    connection.commit()
    return


# which descriptions are still needed
def get_notices_missing_description(connection, today_text, fetch_limit, target_naics_codes=()):
    naics_parameters = {f"naics_{index}": naics_code
                        for index, naics_code in enumerate(target_naics_codes)}
    naics_placeholders = ", ".join(f":{name}" for name in naics_parameters)
    if naics_placeholders:
        naics_filter = f"AND naics_code IN ({naics_placeholders})"
    else:
        naics_filter = ""
    rows = connection.execute(
        f"""SELECT notice_id, description FROM solicitations
        WHERE description_text IS NULL
        AND description LIKE 'http%'
        AND substr(response_deadline, 1, 10) >= :today_text
        {naics_filter}
        ORDER BY posted_date DESC
        LIMIT :fetch_limit""",
        {"today_text": today_text, "fetch_limit": fetch_limit, **naics_parameters}).fetchall()
    return rows


# storing a fetched description
def save_description_text(connection, notice_id, description_text):
    cursor = connection.execute(
        """UPDATE solicitations SET description_text = :description_text
        WHERE notice_id = :notice_id""",
        {"notice_id": notice_id, "description_text": description_text}
    )
    connection.commit()
    return cursor.rowcount


# the fetch, normalize, and store steps combined
async def ingest(client, connection, posted_from, posted_to, **filters) -> int:
    records = [normalize(row) async for row in client.search(posted_from, posted_to, **filters)]
    return upsert(connection, records)


# the notice sync with its bookmark and its own quota rules
async def run_sync(samclient, connection, *, days, max_pages, today, lookback_days, daily_request_limit):
    today_text = today.isoformat()
    requests_used_today = get_requests_used(connection, today_text)
    requests_remaining = daily_request_limit - requests_used_today
    if requests_remaining <= 0:
        print("Daily request limit reached; no request made.")
        return 0

    if max_pages is None:
        page_limit = requests_remaining
    else:
        page_limit = min(max_pages, requests_remaining)

    last_synced_date = get_last_synced_date(connection)
    posted_from = choose_posted_from(
        last_synced_date, today, days, lookback_days)

    requests_before_run = samclient.requests_made
    try:
        try:
            upserted_count = await ingest(samclient, connection, posted_from, today,
                                          limit=1000, max_pages=page_limit)
        finally:
            requests_this_run = samclient.requests_made - requests_before_run
            add_requests_used(connection, today_text, requests_this_run)
    except RateLimitError:
        mark_day_exhausted(connection, today_text, daily_request_limit)
        raise
    if samclient.stopped_early:
        print("Stopped early at the page or quota limit; bookmark not advanced")
    else:
        save_last_synced_date(connection, today)
    return upserted_count


# the description pass with its own quota rules
async def fetch_descriptions(samclient, connection, today, *, daily_request_limit, max_fetches, target_naics_codes=()):
    today_text = today.isoformat()
    requests_remaining = daily_request_limit - \
        get_requests_used(connection, today_text)
    request_budget = min(max_fetches, requests_remaining)
    if request_budget <= 0:
        return 0
    notices_missing_description = get_notices_missing_description(
        connection, today_text, request_budget, target_naics_codes)
    saved_count = 0
    requests_before_run = samclient.requests_made
    try:
        try:
            for notice_id, description_url in notices_missing_description:
                description_text = await samclient.fetch_description(description_url)
                save_description_text(connection, notice_id, description_text)
                saved_count += 1
        finally:
            requests_this_run = samclient.requests_made - requests_before_run
            add_requests_used(connection, today_text, requests_this_run)
    except RateLimitError:
        mark_day_exhausted(connection, today_text, daily_request_limit)
        raise
    return saved_count
