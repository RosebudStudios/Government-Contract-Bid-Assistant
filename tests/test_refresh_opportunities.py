import asyncio
import importlib
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from src import pipeline
from src.ingestion import RateLimitError
from src.pipeline import ingestion_pipeline
from src.tools import (ACTION_RISK, LOCAL_TOOLS, RiskLevel, interrupt_map,
                       needs_human_approval)
from src.tools.refresh_opportunities import RefreshOpportunitiesInput

# `src.tools` re-exports the tool OBJECT under the same name as its module, so a normal
# import would hand back the tool. import_module returns the real module for monkeypatching.
tool_module = importlib.import_module("src.tools.refresh_opportunities")
refresh_opportunities = tool_module.refresh_opportunities

FAR_FUTURE_DEADLINE = "2099-01-01T10:00:00-05:00"


# ------------------------------------------------------------------ fakes and helpers

def make_sam_row(notice_id, description_url=None, naics_code="541512"):
    """A raw SAM.gov search row, in the shape normalize() expects."""
    return {"noticeId": notice_id, "title": f"Title {notice_id}",
            "naicsCode": naics_code, "postedDate": "2026-11-01",
            "responseDeadLine": FAR_FUTURE_DEADLINE,
            "description": description_url}


class FakeSamClient:
    """Stands in for SamClient: serves canned rows and descriptions, records what it was asked."""

    def __init__(self, rows=(), descriptions=None, search_error=None, description_error=None):
        self.rows = list(rows)
        self.descriptions = descriptions or {}
        self.search_error = search_error
        self.description_error = description_error
        self.requests_made = 0
        self.stopped_early = False
        self.closed = False
        self.search_calls = []
        self.description_calls = []

    async def search(self, posted_from, posted_to, *, limit=1000, max_pages=None, **filters):
        self.search_calls.append({"posted_from": posted_from, "posted_to": posted_to,
                                  "max_pages": max_pages})
        self.requests_made += 1
        if self.search_error is not None:
            raise self.search_error
        for row in self.rows:
            yield row

    async def fetch_description(self, description_url):
        self.description_calls.append(description_url)
        self.requests_made += 1
        if self.description_error is not None:
            raise self.description_error
        return self.descriptions[description_url]

    async def aclose(self):
        self.closed = True


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "pipeline_test.db"


@pytest.fixture
def opened_connections(monkeypatch, database_path):
    """Points the pipeline at a temp database and records every connection it opens."""
    monkeypatch.setattr(pipeline, "settings", SimpleNamespace(
        db_path=database_path, sync_lookback_days=1, daily_request_limit=10,
        description_requests_per_run=5, target_naics_codes=("541512",)))
    connections = []
    real_init_db = pipeline.init_db

    def recording_init_db(path):
        connection = real_init_db(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr(pipeline, "init_db", recording_init_db)
    return connections


def use_client(monkeypatch, fake_client):
    monkeypatch.setattr(pipeline, "SamClient", lambda: fake_client)


def run_pipeline(**arguments):
    return asyncio.run(ingestion_pipeline(**arguments))


def query(database_path, sql):
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def assert_connection_closed(connection):
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


# ------------------------------------------------------------------ ingestion_pipeline

def test_the_summary_reports_how_many_notices_and_descriptions_were_saved(
        monkeypatch, opened_connections, database_path):
    fake_client = FakeSamClient(
        rows=[make_sam_row("n1", "https://sam.gov/d1"),
              make_sam_row("n2", "https://sam.gov/d2")],
        descriptions={"https://sam.gov/d1": "First description",
                      "https://sam.gov/d2": "Second description"})
    use_client(monkeypatch, fake_client)
    assert run_pipeline() == "Notices added or updated: 2. Descriptions saved: 2."
    stored = dict(query(
        database_path, "SELECT notice_id, description_text FROM solicitations"))
    assert stored == {"n1": "First description", "n2": "Second description"}


def test_the_quota_used_is_recorded_for_today(monkeypatch, opened_connections, database_path):
    fake_client = FakeSamClient(rows=[make_sam_row("n1", "https://sam.gov/d1")],
                                descriptions={"https://sam.gov/d1": "text"})
    use_client(monkeypatch, fake_client)
    run_pipeline()
    usage = query(database_path, "SELECT day, request_count FROM api_usage")
    # one search page + one description
    assert usage == [(date.today().isoformat(), 2)]


def test_days_sets_how_far_back_the_search_looks(monkeypatch, opened_connections):
    fake_client = FakeSamClient()
    use_client(monkeypatch, fake_client)
    run_pipeline(days=3)
    search_call = fake_client.search_calls[0]
    assert search_call["posted_to"] - \
        search_call["posted_from"] == timedelta(days=3)


def test_max_pages_is_passed_on_as_the_page_limit(monkeypatch, opened_connections):
    fake_client = FakeSamClient()
    use_client(monkeypatch, fake_client)
    run_pipeline(days=1, max_pages=2)
    assert fake_client.search_calls[0]["max_pages"] == 2


def test_without_max_pages_the_search_may_use_the_whole_remaining_quota(
        monkeypatch, opened_connections):
    fake_client = FakeSamClient()
    use_client(monkeypatch, fake_client)
    run_pipeline(days=1, max_pages=None)
    # daily_request_limit
    assert fake_client.search_calls[0]["max_pages"] == 10


def test_only_notices_in_the_target_naics_codes_get_descriptions_fetched(
        monkeypatch, opened_connections):
    fake_client = FakeSamClient(
        rows=[make_sam_row("target", "https://sam.gov/target", naics_code="541512"),
              make_sam_row("other", "https://sam.gov/other", naics_code="999999")],
        descriptions={"https://sam.gov/target": "text", "https://sam.gov/other": "text"})
    use_client(monkeypatch, fake_client)
    run_pipeline()
    assert fake_client.description_calls == ["https://sam.gov/target"]


def test_a_rate_limit_during_the_search_returns_a_message_and_skips_descriptions(
        monkeypatch, opened_connections, database_path):
    fake_client = FakeSamClient(search_error=RateLimitError("429"))
    use_client(monkeypatch, fake_client)
    result = run_pipeline()
    assert "daily request limit" in result and "tomorrow" in result
    assert "Descriptions saved" not in result
    assert fake_client.description_calls == []
    # and the day is marked used up so later runs make no requests
    assert query(database_path, "SELECT request_count FROM api_usage") == [
        (10,)]


def test_a_rate_limit_during_descriptions_keeps_the_notice_count_already_earned(
        monkeypatch, opened_connections):
    fake_client = FakeSamClient(
        rows=[make_sam_row("n1", "https://sam.gov/d1")],
        description_error=RateLimitError("429"))
    use_client(monkeypatch, fake_client)
    result = run_pipeline()
    assert result.startswith("Notices added or updated: 1.")
    assert "daily request limit" in result


def test_the_client_and_connection_are_closed_after_a_normal_run(
        monkeypatch, opened_connections):
    fake_client = FakeSamClient()
    use_client(monkeypatch, fake_client)
    run_pipeline()
    assert fake_client.closed
    assert len(opened_connections) == 1
    assert_connection_closed(opened_connections[0])


def test_the_client_and_connection_are_closed_after_a_rate_limit(
        monkeypatch, opened_connections):
    fake_client = FakeSamClient(search_error=RateLimitError("429"))
    use_client(monkeypatch, fake_client)
    run_pipeline()
    assert fake_client.closed
    assert_connection_closed(opened_connections[0])


def test_other_errors_are_not_swallowed_but_everything_is_still_closed(
        monkeypatch, opened_connections):
    fake_client = FakeSamClient(search_error=RuntimeError("SAM.gov error 500"))
    use_client(monkeypatch, fake_client)
    with pytest.raises(RuntimeError, match="500"):
        run_pipeline()
    assert fake_client.closed
    assert_connection_closed(opened_connections[0])


# ------------------------------------------------------------------ the refresh_opportunities tool

def install_fake_pipeline(monkeypatch, *, result="done", error=None):
    """Replaces the pipeline the tool calls; returns the list of keyword arguments it received."""
    received_calls = []

    async def fake_pipeline(days, max_pages):
        received_calls.append({"days": days, "max_pages": max_pages})
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(tool_module, "ingestion_pipeline", fake_pipeline)
    return received_calls


def invoke_tool(tool_arguments):
    return asyncio.run(refresh_opportunities.ainvoke(tool_arguments))


def test_the_tool_passes_its_arguments_to_the_pipeline_and_returns_its_summary(monkeypatch):
    received_calls = install_fake_pipeline(
        monkeypatch, result="Notices added or updated: 4.")
    result = invoke_tool({"days": 3, "max_pages": 2})
    assert received_calls == [{"days": 3, "max_pages": 2}]
    assert result == "Notices added or updated: 4."


def test_the_tool_defaults_to_one_day_and_no_page_limit(monkeypatch):
    received_calls = install_fake_pipeline(monkeypatch)
    invoke_tool({})
    assert received_calls == [{"days": 1, "max_pages": None}]


@pytest.mark.parametrize("failure", [
    RuntimeError("SAM.gov error 500: internal detail"),
    httpx.ConnectError("internal detail"),
    httpx.ReadTimeout("internal detail"),
])
def test_known_failures_become_a_short_message_instead_of_an_exception(monkeypatch, failure):
    install_fake_pipeline(monkeypatch, error=failure)
    result = invoke_tool({})
    assert result.startswith(f"The refresh failed ({type(failure).__name__})")
    assert "try again later" in result
    # raw error text never reaches the model
    assert "internal detail" not in result


def test_unexpected_errors_are_not_hidden(monkeypatch):
    install_fake_pipeline(monkeypatch, error=ValueError("a real bug"))
    with pytest.raises(ValueError, match="a real bug"):
        invoke_tool({})


def test_the_tool_has_the_expected_name_and_schema():
    assert refresh_opportunities.name == "refresh_opportunities"
    assert refresh_opportunities.args_schema is RefreshOpportunitiesInput


def test_the_input_defaults():
    arguments = RefreshOpportunitiesInput()
    assert arguments.days == 1
    assert arguments.max_pages is None


@pytest.mark.parametrize("bad_arguments", [
    {"days": 0}, {"days": 8}, {"max_pages": 0}, {"max_pages": 8},
])
def test_out_of_range_arguments_are_rejected(bad_arguments):
    with pytest.raises(ValidationError):
        RefreshOpportunitiesInput(**bad_arguments)


@pytest.mark.parametrize("good_arguments", [
    {"days": 1}, {"days": 7}, {"max_pages": 1}, {
        "max_pages": 7}, {"max_pages": None},
])
def test_the_edges_of_the_allowed_ranges_are_accepted(good_arguments):
    RefreshOpportunitiesInput(**good_arguments)


# ------------------------------------------------------------------ registration and approval

def test_the_tool_is_registered_with_the_agent():
    assert refresh_opportunities in LOCAL_TOOLS


def test_the_tool_is_rated_medium_so_it_needs_human_approval():
    assert ACTION_RISK["refresh_opportunities"] is RiskLevel.MEDIUM
    assert needs_human_approval("refresh_opportunities")


def test_the_approval_gate_covers_only_this_tool_among_the_local_tools():
    assert set(interrupt_map(LOCAL_TOOLS)) == {"refresh_opportunities"}
