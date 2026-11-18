import asyncio
from datetime import date, timedelta

import httpx
import pytest

from src.ingestion import (
    SamClient,
    add_requests_used,
    get_requests_used,
    choose_posted_from,
    get_last_synced_date,
    RateLimitError,
    fetch_descriptions,
    mark_day_exhausted,
    ingest,
    upsert,
    save_description_text,
    init_db,
    parse_args,
    run_sync,
    save_last_synced_date,
)

TODAY = date(2026, 10, 28)
API_KEY = "test-api-key-12345"


# ---------------------------------------------------------------------------
# Test helpers: a fake SAM.gov server and a few small shortcuts
# ---------------------------------------------------------------------------

class FakeSam:
    """Pretends to be SAM.gov. Serves `total_records` notices, `limit` per page,
    and remembers every request it received so tests can inspect them."""

    def __init__(self, total_records=0, status_code=200, response_text="", description_body=None,
                 description_bodies=None, rate_limit_descriptions_after=None):
        self.description_bodies = description_bodies or {}          # body per noticeid
        self.rate_limit_descriptions_after = rate_limit_descriptions_after
        self.description_request_count = 0
        # raw text served by the noticedesc route
        self.description_body = description_body
        self.total_records = total_records
        self.status_code = status_code
        self.response_text = response_text
        self.requests = []          # one dict of query parameters per request

    def handle(self, request: httpx.Request) -> httpx.Response:
        parameters = dict(request.url.params)
        self.requests.append(parameters)
        if self.status_code != 200:
            return httpx.Response(self.status_code, text=self.response_text)
        if request.url.path.endswith("/noticedesc"):
            self.description_request_count += 1
            if (self.rate_limit_descriptions_after is not None
                    and self.description_request_count > self.rate_limit_descriptions_after):
                return httpx.Response(429, text="quota exceeded")
            body = self.description_bodies.get(
                parameters["noticeid"], self.description_body)
            return httpx.Response(200, text=body or "")
        limit = int(parameters["limit"])
        offset = int(parameters["offset"])
        first_index = offset * limit              # offset is a page number
        last_index = min(first_index + limit, self.total_records)
        rows = [make_row(index) for index in range(first_index, last_index)]
        return httpx.Response(
            200, json={"totalRecords": self.total_records, "opportunitiesData": rows})


def make_row(index):
    """A minimal raw SAM notice. normalize() tolerates missing fields."""
    return {"noticeId": f"notice-{index}", "title": f"Title {index}",
            "postedDate": "2026-10-27"}


def make_client(fake_sam):
    transport = httpx.MockTransport(fake_sam.handle)
    return SamClient(api_key=API_KEY, client=httpx.AsyncClient(transport=transport))


async def collect(samclient, **search_arguments):
    """Run a search to the end, return every raw row, and close the client."""
    try:
        return [row async for row in samclient.search(
            TODAY - timedelta(days=1), TODAY, **search_arguments)]
    finally:
        await samclient.aclose()


async def sync_with(fake_sam, connection, *, today=TODAY, days=3, max_pages=None, lookback_days=1,
                    daily_request_limit=1000):
    samclient = make_client(fake_sam)
    try:
        return await run_sync(samclient, connection, days=days, max_pages=max_pages,
                              today=today, lookback_days=lookback_days,
                              daily_request_limit=daily_request_limit)
    finally:
        await samclient.aclose()


def count_rows(connection, table="solicitations"):
    return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def connection():
    database = init_db(":memory:")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# The bookmark: get_last_synced_date / save_last_synced_date
# ---------------------------------------------------------------------------

def test_bookmark_is_none_before_anything_is_saved(connection):
    assert get_last_synced_date(connection) is None


def test_saved_bookmark_round_trips_as_a_date(connection):
    save_last_synced_date(connection, date(2026, 10, 27))
    assert get_last_synced_date(connection) == date(2026, 10, 27)


def test_saving_twice_keeps_one_row_with_the_latest_date(connection):
    save_last_synced_date(connection, date(2026, 10, 26))
    save_last_synced_date(connection, date(2026, 10, 27))
    assert count_rows(connection, "sync_state") == 1
    assert get_last_synced_date(connection) == date(2026, 10, 27)


def test_bookmark_survives_closing_and_reopening(tmp_path):
    # catches a missing commit(): uncommitted data vanishes when the connection closes
    database_path = tmp_path / "bookmark.db"
    first_connection = init_db(database_path)
    save_last_synced_date(first_connection, date(2026, 10, 27))
    first_connection.close()
    second_connection = init_db(database_path)
    assert get_last_synced_date(second_connection) == date(2026, 10, 27)
    second_connection.close()


# ---------------------------------------------------------------------------
# choose_posted_from: a pure function, so no database or network needed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "last_synced_date, days, lookback_days, expected_start",
    [
        # first run: today minus days
        (None, 3, 1, date(2026, 10, 25)),
        (date(2026, 10, 27), 3, 1, date(2026, 10, 26)),   # bookmark minus lookback
        (date(2026, 10, 20), 3, 2, date(2026, 10, 18)),   # bigger lookback
        # old bookmark: clamped to 364 days back
        (date(2024, 1, 1), 3, 1, date(2025, 10, 29)),
        # huge --days: clamped too
        (None, 400, 1, date(2025, 10, 29)),
    ],
)
def test_choose_posted_from(last_synced_date, days, lookback_days, expected_start):
    assert choose_posted_from(last_synced_date, TODAY,
                              days, lookback_days) == expected_start


# ---------------------------------------------------------------------------
# SamClient.search: paging, the page cap, and error handling
# ---------------------------------------------------------------------------

def test_search_returns_everything_from_a_single_page():
    fake_sam = FakeSam(total_records=3)
    rows = asyncio.run(collect(make_client(fake_sam), limit=10))
    assert [row["noticeId"] for row in rows] == [
        "notice-0", "notice-1", "notice-2"]
    assert len(fake_sam.requests) == 1


def test_search_follows_pages_until_all_records_are_fetched():
    fake_sam = FakeSam(total_records=5)
    samclient = make_client(fake_sam)
    rows = asyncio.run(collect(samclient, limit=2))
    assert len(rows) == 5
    assert len({row["noticeId"] for row in rows}) == 5          # no duplicates
    assert [request["offset"]
            for request in fake_sam.requests] == ["0", "1", "2"]
    assert samclient.stopped_early is False


def test_search_sends_key_dates_limit_and_filters():
    fake_sam = FakeSam(total_records=1)
    asyncio.run(collect(make_client(fake_sam), limit=7, ptype="o,k"))
    request = fake_sam.requests[0]
    assert request["api_key"] == API_KEY
    assert request["postedFrom"] == "10/27/2026"
    assert request["postedTo"] == "10/28/2026"
    assert request["limit"] == "7"
    assert request["ptype"] == "o,k"


def test_page_cap_stops_early_and_says_so():
    fake_sam = FakeSam(total_records=5)
    samclient = make_client(fake_sam)
    rows = asyncio.run(collect(samclient, limit=2, max_pages=1))
    assert len(rows) == 2
    assert len(fake_sam.requests) == 1
    assert samclient.stopped_early is True


def test_page_cap_that_exactly_covers_all_data_is_not_an_early_stop():
    # boundary case: 4 records at 2 per page is exactly 2 pages, so a cap of 2 loses nothing
    fake_sam = FakeSam(total_records=4)
    samclient = make_client(fake_sam)
    rows = asyncio.run(collect(samclient, limit=2, max_pages=2))
    assert len(rows) == 4
    assert samclient.stopped_early is False


def test_stopped_early_resets_when_the_same_client_searches_again():
    async def scenario():
        fake_sam = FakeSam(total_records=5)
        samclient = make_client(fake_sam)
        start, end = TODAY - timedelta(days=1), TODAY
        [row async for row in samclient.search(start, end, limit=2, max_pages=1)]
        assert samclient.stopped_early is True
        [row async for row in samclient.search(start, end, limit=2)]
        result = samclient.stopped_early
        await samclient.aclose()
        return result
    assert asyncio.run(scenario()) is False


def test_a_404_means_no_results_not_an_error():
    fake_sam = FakeSam(status_code=404)
    samclient = make_client(fake_sam)
    assert asyncio.run(collect(samclient)) == []
    assert samclient.stopped_early is False


def test_rate_limit_raises_a_clear_error_without_leaking_the_key():
    fake_sam = FakeSam(status_code=429, response_text="quota exceeded")
    with pytest.raises(RuntimeError, match="429") as error_info:
        asyncio.run(collect(make_client(fake_sam)))
    assert API_KEY not in str(error_info.value)


def test_server_error_raises_without_leaking_the_key():
    fake_sam = FakeSam(status_code=500)
    with pytest.raises(RuntimeError, match="500") as error_info:
        asyncio.run(collect(make_client(fake_sam)))
    assert API_KEY not in str(error_info.value)
    # `from None` hides the original error, whose message contains the full URL (and the key)
    assert error_info.value.__suppress_context__ is True


# ---------------------------------------------------------------------------
# ingest and run_sync: the whole flow, with a fake server and an in-memory database
# ---------------------------------------------------------------------------

def test_ingest_normalizes_and_stores_the_records(connection):
    async def scenario():
        samclient = make_client(FakeSam(total_records=3))
        try:
            return await ingest(samclient, connection, TODAY - timedelta(days=1), TODAY, limit=10)
        finally:
            await samclient.aclose()
    assert asyncio.run(scenario()) == 3
    row = connection.execute(
        "SELECT notice_id, title, posted_date FROM solicitations WHERE notice_id = 'notice-1'"
    ).fetchone()
    # snake_case names prove normalize ran
    assert row == ("notice-1", "Title 1", "2026-10-27")


def test_first_complete_run_stores_everything_and_saves_the_bookmark(connection):
    fake_sam = FakeSam(total_records=5)
    upserted_count = asyncio.run(sync_with(fake_sam, connection, days=3))
    assert upserted_count == 5
    assert count_rows(connection) == 5
    # today minus 3 days
    assert fake_sam.requests[0]["postedFrom"] == "10/25/2026"
    assert get_last_synced_date(connection) == TODAY


def test_next_run_starts_from_the_bookmark_minus_the_lookback(connection):
    save_last_synced_date(connection, date(2026, 10, 27))
    fake_sam = FakeSam(total_records=1)
    asyncio.run(sync_with(fake_sam, connection, days=30, lookback_days=1))
    # not 30 days back
    assert fake_sam.requests[0]["postedFrom"] == "10/26/2026"


def test_running_the_same_sync_twice_does_not_duplicate_rows(connection):
    asyncio.run(sync_with(FakeSam(total_records=5), connection))
    asyncio.run(sync_with(FakeSam(total_records=5), connection))
    assert count_rows(connection) == 5


def test_a_capped_run_stores_what_it_got_but_does_not_advance_the_bookmark(connection):
    fake_sam = FakeSam(total_records=2500)
    # limit is 1000 inside run_sync, so max_pages=1 stops after 1000 of 2500 records
    upserted_count = asyncio.run(sync_with(fake_sam, connection, max_pages=1))
    assert upserted_count == 1000
    assert get_last_synced_date(connection) is None


def test_a_capped_run_leaves_an_existing_bookmark_unchanged(connection):
    save_last_synced_date(connection, date(2026, 10, 20))
    asyncio.run(sync_with(FakeSam(total_records=2500), connection, max_pages=1))
    assert get_last_synced_date(connection) == date(2026, 10, 20)


def test_a_failed_run_does_not_advance_the_bookmark(connection):
    with pytest.raises(RuntimeError):
        asyncio.run(sync_with(FakeSam(status_code=429), connection))
    assert get_last_synced_date(connection) is None


def test_a_day_with_no_notices_still_advances_the_bookmark(connection):
    asyncio.run(sync_with(FakeSam(status_code=404), connection))
    assert get_last_synced_date(connection) == TODAY
    assert count_rows(connection) == 0


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

def test_parse_args_defaults():
    args = parse_args([])
    assert args.days == 1
    assert args.max_pages is None


def test_parse_args_reads_both_options():
    args = parse_args(["--days", "7", "--max-pages", "2"])
    assert args.days == 7
    assert args.max_pages == 2


@pytest.mark.parametrize("bad_days", ["0", "365", "-1"])
def test_parse_args_rejects_days_outside_1_to_364(bad_days):
    with pytest.raises(SystemExit):
        parse_args(["--days", bad_days])


# ---------------------------------------------------------------------------
# The daily request counter
# ---------------------------------------------------------------------------

TODAY_TEXT = TODAY.isoformat()


def test_requests_used_is_zero_for_a_day_with_no_row(connection):
    assert get_requests_used(connection, TODAY_TEXT) == 0


def test_adding_requests_accumulates_within_a_day_and_not_across_days(connection):
    add_requests_used(connection, "2026-10-28", 3)
    add_requests_used(connection, "2026-10-28", 4)
    add_requests_used(connection, "2026-10-29", 2)
    assert get_requests_used(connection, "2026-10-28") == 7
    assert get_requests_used(connection, "2026-10-29") == 2


def test_requests_used_survives_closing_and_reopening(tmp_path):
    database_path = tmp_path / "usage.db"
    first_connection = init_db(database_path)
    add_requests_used(first_connection, TODAY_TEXT, 5)
    first_connection.close()
    second_connection = init_db(database_path)
    assert get_requests_used(second_connection, TODAY_TEXT) == 5
    second_connection.close()


def test_usage_recorded_equals_the_requests_the_server_saw(connection):
    fake_sam = FakeSam(total_records=2500)           # 3 pages of up to 1000
    asyncio.run(sync_with(fake_sam, connection))
    assert len(fake_sam.requests) == 3
    assert get_requests_used(connection, TODAY_TEXT) == 3


def test_quota_caps_the_number_of_requests_and_keeps_the_bookmark(connection):
    fake_sam = FakeSam(total_records=5000)           # would need 5 pages
    asyncio.run(sync_with(fake_sam, connection, daily_request_limit=3))
    assert len(fake_sam.requests) == 3
    assert get_requests_used(connection, TODAY_TEXT) == 3
    assert get_last_synced_date(connection) is None


def test_exhausted_quota_makes_no_request_at_all(connection):
    add_requests_used(connection, TODAY_TEXT, 3)
    fake_sam = FakeSam(total_records=5000)
    upserted_count = asyncio.run(
        sync_with(fake_sam, connection, daily_request_limit=3))
    assert upserted_count == 0
    assert fake_sam.requests == []
    assert get_last_synced_date(connection) is None


def test_a_failed_run_still_records_the_requests_it_made(connection):
    with pytest.raises(RuntimeError):
        asyncio.run(sync_with(FakeSam(status_code=500), connection))
    assert get_requests_used(connection, TODAY_TEXT) == 1


def test_a_smaller_user_page_cap_wins_over_the_remaining_quota(connection):
    fake_sam = FakeSam(total_records=5000)
    asyncio.run(sync_with(fake_sam, connection,
                max_pages=2, daily_request_limit=10))
    assert len(fake_sam.requests) == 2


def test_a_second_run_the_same_day_only_gets_what_is_left(connection):
    asyncio.run(sync_with(FakeSam(total_records=3000),
                connection, daily_request_limit=5))   # uses 3
    second_server = FakeSam(total_records=5000)
    asyncio.run(sync_with(second_server, connection,
                daily_request_limit=5))                 # only 2 left
    assert len(second_server.requests) == 2
    assert get_requests_used(connection, TODAY_TEXT) == 5


def test_a_larger_user_page_cap_is_cut_down_to_the_remaining_quota(connection):
    fake_sam = FakeSam(total_records=5000)           # would need 5 pages
    asyncio.run(sync_with(fake_sam, connection,
                max_pages=10, daily_request_limit=3))
    assert len(fake_sam.requests) == 3


# ---------------------------------------------------------------------------
# SamClient.fetch_description
# ---------------------------------------------------------------------------

DESCRIPTION_URL = "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=abc123"


def fetch_description_from(fake_sam):
    async def scenario():
        samclient = make_client(fake_sam)
        try:
            return await samclient.fetch_description(DESCRIPTION_URL), samclient.requests_made
        finally:
            await samclient.aclose()
    return asyncio.run(scenario())


def test_fetch_description_returns_the_text_from_the_json_body():
    fake_sam = FakeSam(
        description_body='{"description": "<p>Hello &amp; welcome</p>"}')
    description_text, _ = fetch_description_from(fake_sam)
    assert description_text == "<p>Hello &amp; welcome</p>"


def test_fetch_description_sends_both_the_notice_id_and_the_key():
    # httpx replaces the query string if you pass params=..., which would drop noticeid
    fake_sam = FakeSam(description_body='{"description": "x"}')
    fetch_description_from(fake_sam)
    assert fake_sam.requests == [{"noticeid": "abc123", "api_key": API_KEY}]


def test_fetch_description_counts_one_request():
    fake_sam = FakeSam(description_body='{"description": "x"}')
    _, requests_made = fetch_description_from(fake_sam)
    assert requests_made == 1


@pytest.mark.parametrize("body", [
    '{"description": null}',
    '{"description": ""}',
    '{"description": "   "}',
    '{"description": "Description Not Found"}',
    '{"other": 1}',
    '[]',
    'not json at all',
    '',
])
def test_fetch_description_treats_missing_or_empty_descriptions_as_empty_text(body):
    description_text, _ = fetch_description_from(
        FakeSam(description_body=body))
    assert description_text == ""


def test_fetch_description_treats_a_404_as_empty_text():
    description_text, requests_made = fetch_description_from(
        FakeSam(status_code=404))
    assert description_text == ""
    assert requests_made == 1


def test_fetch_description_rate_limit_raises_without_leaking_the_key():
    with pytest.raises(RuntimeError, match="429") as error_info:
        fetch_description_from(
            FakeSam(status_code=429, response_text="quota exceeded"))
    assert API_KEY not in str(error_info.value)


def test_fetch_description_server_error_raises_without_leaking_the_key():
    with pytest.raises(RuntimeError, match="500") as error_info:
        fetch_description_from(FakeSam(status_code=500))
    assert API_KEY not in str(error_info.value)
    assert error_info.value.__suppress_context__ is True


# ---------------------------------------------------------------------------
# fetch_descriptions
# ---------------------------------------------------------------------------

def add_notices(connection, notice_ids):
    records = [{"notice_id": notice_id, "title": "T", "posted_date": "2026-10-20",
                "response_deadline": "2026-11-05T12:00:00-05:00",
                "description": f"https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid={notice_id}"}
               for notice_id in notice_ids]
    upsert(connection, records)


def stored_description_texts(connection):
    return dict(connection.execute("SELECT notice_id, description_text FROM solicitations"))


async def describe_with(fake_sam, connection, *, daily_request_limit=1000, max_fetches=100):
    samclient = make_client(fake_sam)
    try:
        return await fetch_descriptions(samclient, connection, TODAY,
                                        daily_request_limit=daily_request_limit,
                                        max_fetches=max_fetches)
    finally:
        await samclient.aclose()


def test_fetch_descriptions_saves_the_text_for_every_missing_notice(connection):
    add_notices(connection, ["a", "b", "c"])
    fake_sam = FakeSam(description_bodies={
        "a": '{"description": "text a"}', "b": '{"description": "text b"}', "c": '{"description": "text c"}'})
    saved_count = asyncio.run(describe_with(fake_sam, connection))
    assert saved_count == 3
    assert stored_description_texts(connection) == {
        "a": "text a", "b": "text b", "c": "text c"}


def test_fetch_descriptions_skips_notices_that_already_have_text(connection):
    add_notices(connection, ["a", "b"])
    save_description_text(connection, "a", "already here")
    fake_sam = FakeSam(description_body='{"description": "new"}')
    asyncio.run(describe_with(fake_sam, connection))
    assert [request["noticeid"] for request in fake_sam.requests] == ["b"]
    assert stored_description_texts(connection)["a"] == "already here"


def test_usage_recorded_is_the_requests_actually_made_not_the_budget(connection):
    # only 2 notices, budget is 10
    add_notices(connection, ["a", "b"])
    asyncio.run(describe_with(FakeSam(description_body='{"description": "x"}'), connection,
                              max_fetches=10))
    assert get_requests_used(connection, TODAY_TEXT) == 2


def test_fetching_stops_at_the_max_fetches_cap(connection):
    add_notices(connection, ["a", "b", "c", "d"])
    fake_sam = FakeSam(description_body='{"description": "x"}')
    assert asyncio.run(describe_with(fake_sam, connection, max_fetches=2)) == 2
    assert len(fake_sam.requests) == 2


def test_fetching_stops_at_the_remaining_daily_quota(connection):
    add_notices(connection, ["a", "b", "c", "d"])
    # 1 of 3 already spent
    add_requests_used(connection, TODAY_TEXT, 1)
    fake_sam = FakeSam(description_body='{"description": "x"}')
    asyncio.run(describe_with(fake_sam, connection, daily_request_limit=3))
    assert len(fake_sam.requests) == 2
    assert get_requests_used(connection, TODAY_TEXT) == 3


# exactly used up, and over
@pytest.mark.parametrize("already_used", [3, 5])
def test_no_requests_when_the_quota_is_used_up(connection, already_used):
    # the over-the-limit case catches a negative budget, which SQLite treats as "no LIMIT"
    add_notices(connection, ["a", "b"])
    add_requests_used(connection, TODAY_TEXT, already_used)
    fake_sam = FakeSam(description_body='{"description": "x"}')
    assert asyncio.run(describe_with(
        fake_sam, connection, daily_request_limit=3)) == 0
    assert fake_sam.requests == []


def test_a_notice_with_no_description_is_stored_as_empty_and_never_retried(connection):
    add_notices(connection, ["a"])
    asyncio.run(describe_with(
        FakeSam(description_body='{"description": null}'), connection))
    assert stored_description_texts(connection) == {"a": ""}
    second_server = FakeSam(description_body='{"description": "x"}')
    asyncio.run(describe_with(second_server, connection))
    assert second_server.requests == []


def test_a_rate_limit_midway_keeps_earlier_texts_and_still_records_usage(connection):
    add_notices(connection, ["a", "b", "c"])
    fake_sam = FakeSam(
        description_body='{"description": "x"}', rate_limit_descriptions_after=1)
    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(describe_with(
            fake_sam, connection, daily_request_limit=10))
    texts = stored_description_texts(connection)
    assert sum(text == "x" for text in texts.values()) == 1
    assert sum(text is None for text in texts.values()) == 2
    # a 429 marks the whole day as used up
    assert get_requests_used(connection, TODAY_TEXT) == 10


# ---------------------------------------------------------------------------
# A 429 means SAM's daily quota is gone, whatever our own counter says
# ---------------------------------------------------------------------------

def test_rate_limit_error_is_still_a_runtime_error():
    assert issubclass(RateLimitError, RuntimeError)


def test_a_429_during_the_sync_marks_the_whole_day_as_used_up(connection):
    with pytest.raises(RateLimitError):
        asyncio.run(sync_with(FakeSam(status_code=429),
                    connection, daily_request_limit=10))
    assert get_requests_used(
        connection, TODAY_TEXT) == 10      # not 1, and not 11


def test_after_a_429_the_next_run_makes_no_request(connection):
    with pytest.raises(RateLimitError):
        asyncio.run(sync_with(FakeSam(status_code=429),
                    connection, daily_request_limit=10))
    second_server = FakeSam(total_records=5)
    assert asyncio.run(sync_with(second_server, connection,
                       daily_request_limit=10)) == 0
    assert second_server.requests == []


def test_a_429_during_descriptions_marks_the_whole_day_as_used_up(connection):
    add_notices(connection, ["a", "b"])
    fake_sam = FakeSam(
        description_body='{"description": "x"}', rate_limit_descriptions_after=0)
    with pytest.raises(RateLimitError):
        asyncio.run(describe_with(
            fake_sam, connection, daily_request_limit=10))
    assert get_requests_used(connection, TODAY_TEXT) == 10


def test_a_server_error_does_not_mark_the_day_as_used_up(connection):
    with pytest.raises(RuntimeError):
        asyncio.run(sync_with(FakeSam(status_code=500),
                    connection, daily_request_limit=10))
    assert get_requests_used(connection, TODAY_TEXT) == 1


def test_mark_day_exhausted_raises_the_count_but_never_lowers_it(connection):
    mark_day_exhausted(connection, "2026-10-28", 10)            # no row yet
    assert get_requests_used(connection, "2026-10-28") == 10
    add_requests_used(connection, "2026-10-29", 3)
    # lower count is raised
    mark_day_exhausted(connection, "2026-10-29", 10)
    assert get_requests_used(connection, "2026-10-29") == 10
    add_requests_used(connection, "2026-10-30", 15)
    # higher count is kept
    mark_day_exhausted(connection, "2026-10-30", 10)
    assert get_requests_used(connection, "2026-10-30") == 15


def test_an_exhausted_day_survives_closing_and_reopening(tmp_path):
    database_path = tmp_path / "exhausted.db"
    first_connection = init_db(database_path)
    mark_day_exhausted(first_connection, TODAY_TEXT, 10)
    first_connection.close()
    second_connection = init_db(database_path)
    assert get_requests_used(second_connection, TODAY_TEXT) == 10
    second_connection.close()
