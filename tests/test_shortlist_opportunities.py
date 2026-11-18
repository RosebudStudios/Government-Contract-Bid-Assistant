import importlib
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.company_profile import CompanyProfile
from src.ingestion import init_db, upsert
from src.tools import ACTION_RISK, LOCAL_TOOLS, RiskLevel, needs_human_approval
from src.tools.shortlist_opportunities import (MAX_RESULTS, ShortlistOpportunitiesInput,
                                               fetch_open_notices, format_shortlist,
                                               shortlist_notices)

# src.tools re-exports the tool OBJECT under the same name as its module; import_module
# returns the real module so its `settings` can be monkeypatched.
tool_module = importlib.import_module("src.tools.shortlist_opportunities")
shortlist_opportunities = tool_module.shortlist_opportunities

TODAY = date(2026, 11, 14)


def make_profile(**overrides):
    values = {"name": "Test Co", "naics_codes": ["541512"], "set_aside_eligibilities": ["SBA"],
              "states_served": ["Texas"], "notice_types": ["Solicitation"],
              "minimum_days_to_respond": 5}
    values.update(overrides)
    return CompanyProfile(**values)


def store_notice(connection, notice_id, *, days_until_deadline=30, naics_code="541512",
                 set_aside="SBA", state="Texas", notice_type="Solicitation",
                 posted_date="2026-11-01", title=None, description_text="plain description",
                 today=TODAY):
    if days_until_deadline is None:
        response_deadline = None
    else:
        deadline_date = today + timedelta(days=days_until_deadline)
        response_deadline = f"{deadline_date.isoformat()}T17:00:00-05:00"
    upsert(connection, [{
        "notice_id": notice_id, "title": title or f"Title {notice_id}", "type": notice_type,
        "posted_date": posted_date, "response_deadline": response_deadline,
        "naics_code": naics_code, "type_of_set_aside": set_aside, "pop_state": state,
        "description_text": description_text}])


@pytest.fixture
def connection():
    database = init_db(":memory:")
    yield database
    database.close()


def shortlist(connection, profile=None, **options):
    return shortlist_notices(connection, profile or make_profile(), today=TODAY, **options)


def shortlisted_ids(result):
    return [entry.notice["notice_id"] for entry in result.shortlisted]


# ------------------------------------------------------------------ which notices are scanned

def test_only_open_notices_are_scanned(connection):
    store_notice(connection, "open", days_until_deadline=10)
    store_notice(connection, "closes-today", days_until_deadline=0)
    store_notice(connection, "expired", days_until_deadline=-1)
    store_notice(connection, "no-deadline", days_until_deadline=None)
    notices = fetch_open_notices(connection, today=TODAY)
    assert sorted(notice["notice_id"]
                  for notice in notices) == ["closes-today", "open"]


def test_an_explicit_naics_code_narrows_the_scan(connection):
    store_notice(connection, "a", naics_code="541512")
    store_notice(connection, "b", naics_code="541511")
    result = shortlist(connection, make_profile(
        naics_codes=[]), naics_code="541511")
    assert result.scanned_count == 1 and shortlisted_ids(result) == ["b"]


def test_a_keyword_matches_the_title_or_the_description(connection):
    store_notice(connection, "in-title", title="Cloud migration services")
    store_notice(connection, "in-description",
                 description_text="we need cloud migration help")
    store_notice(connection, "neither")
    result = shortlist(connection, keyword="cloud migration")
    assert sorted(shortlisted_ids(result)) == ["in-description", "in-title"]


def test_the_scan_stops_at_the_cap_and_says_so(connection, monkeypatch):
    monkeypatch.setattr(tool_module, "MAX_NOTICES_SCANNED", 3)
    for index in range(5):
        store_notice(connection, f"n{index}")
    result = shortlist(connection)
    assert result.scanned_count == 3
    assert "scan limit was reached" in format_shortlist(result)


# ------------------------------------------------------------------ hard-check filtering

def test_notices_that_fail_a_hard_check_are_dropped_and_counted_by_check(connection):
    store_notice(connection, "good")
    store_notice(connection, "wrong-state", state="Ohio")
    store_notice(connection, "wrong-set-aside", set_aside="WOSB")
    store_notice(connection, "wrong-state-and-set-aside",
                 state="Ohio", set_aside="WOSB")
    result = shortlist(connection)
    assert shortlisted_ids(result) == ["good"]
    assert result.scanned_count == 4 and result.failed_notice_count == 3
    assert result.failed_counts == {"states_served": 2, "set_aside": 2}


def test_a_notice_closing_soon_is_open_but_fails_the_deadline_check(connection):
    store_notice(connection, "four-days", days_until_deadline=4)
    store_notice(connection, "five-days", days_until_deadline=5)
    result = shortlist(connection)
    assert shortlisted_ids(result) == ["five-days"]
    assert result.failed_counts == {"deadline": 1}
    assert result.scanned_count == 2   # the late one was scanned, not silently skipped


def test_a_failed_set_aside_or_notice_type_is_dropped(connection):
    store_notice(connection, "wosb", set_aside="WOSB")
    store_notice(connection, "sources-sought", notice_type="Sources Sought")
    result = shortlist(connection)
    assert result.shortlisted == []
    assert result.failed_counts == {"set_aside": 1, "type": 1}


# ------------------------------------------------------------------ NAICS scope

def test_by_default_only_the_companys_naics_codes_are_scanned(connection):
    store_notice(connection, "listed", naics_code="541512")
    store_notice(connection, "other", naics_code="335311")
    result = shortlist(connection)
    assert result.scanned_count == 1 and shortlisted_ids(result) == ["listed"]


def test_a_notice_with_no_naics_code_is_scanned_and_ranked_as_unknown(connection):
    store_notice(connection, "listed", naics_code="541512",
                 posted_date="2026-10-01")
    store_notice(connection, "no-naics", naics_code=None,
                 posted_date="2026-11-10")
    result = shortlist(connection)
    assert shortlisted_ids(result) == ["listed", "no-naics"]
    assert "naics_code (no naics code listed)" in format_shortlist(result)


def test_an_explicit_naics_code_overrides_the_company_codes(connection):
    store_notice(connection, "listed", naics_code="541512")
    store_notice(connection, "other", naics_code="541511")
    result = shortlist(connection, naics_code="541511")
    # 541511 is scanned, but the profile does not serve it
    assert shortlisted_ids(result) == []
    assert result.scanned_count == 1 and result.failed_counts == {
        "naics_code": 1}


def test_a_company_with_no_naics_codes_scans_every_open_notice(connection):
    store_notice(connection, "a", naics_code="541512")
    store_notice(connection, "b", naics_code="335311")
    result = shortlist(connection, make_profile(naics_codes=[]))
    assert sorted(shortlisted_ids(result)) == ["a", "b"]


def test_notices_of_other_codes_cannot_fill_the_scan_cap(connection, monkeypatch):
    monkeypatch.setattr(tool_module, "MAX_NOTICES_SCANNED", 3)
    for index in range(5):   # newer, so they would fill a 3-notice scan if they were included
        store_notice(
            connection, f"other{index}", naics_code="335311", posted_date="2026-11-10")
    store_notice(connection, "mine-1", posted_date="2026-10-01")
    store_notice(connection, "mine-2", posted_date="2026-10-02")
    assert sorted(shortlisted_ids(shortlist(connection))) == [
        "mine-1", "mine-2"]


def test_the_output_says_which_naics_codes_were_considered(connection):
    store_notice(connection, "a")
    assert "company's NAICS codes (541512)" in format_shortlist(
        shortlist(connection))
    assert "Only NAICS code 541511 was considered." in format_shortlist(
        shortlist(connection, naics_code="541511"))


# ------------------------------------------------------------------ ranking and limits

def test_notices_with_unknown_fields_rank_after_fully_passing_ones(connection):
    store_notice(connection, "unknown-state",
                 state=None, posted_date="2026-11-12")
    store_notice(connection, "all-pass", posted_date="2026-11-01")
    result = shortlist(connection)
    assert shortlisted_ids(result) == ["all-pass", "unknown-state"]
    assert (result.passed_all_count, result.unknown_only_count) == (1, 1)


def test_the_unknown_field_is_named_in_the_output(connection):
    store_notice(connection, "unknown-state", state=None)
    output = format_shortlist(shortlist(connection))
    assert "unknown: states_served (no place of performance listed)" in output
    assert "state: not listed" in output


def test_ties_are_broken_by_newest_posted_first(connection):
    store_notice(connection, "older", posted_date="2026-10-01")
    store_notice(connection, "newest", posted_date="2026-11-10")
    store_notice(connection, "middle", posted_date="2026-10-20")
    assert shortlisted_ids(shortlist(connection)) == [
        "newest", "middle", "older"]


def test_the_limit_caps_the_results_but_not_the_counts(connection):
    for index in range(4):
        store_notice(connection, f"n{index}")
    result = shortlist(connection, limit=2)
    assert len(result.shortlisted) == 2
    assert result.passed_all_count == 4


def test_a_huge_limit_is_clamped_to_the_maximum(connection):
    for index in range(MAX_RESULTS + 3):
        store_notice(connection, f"n{index:02d}")
    assert len(shortlist(connection, limit=500).shortlisted) == MAX_RESULTS


def test_no_survivors_gives_a_clear_message_and_the_failure_counts(connection):
    store_notice(connection, "a", state="Ohio")
    output = format_shortlist(shortlist(connection))
    assert "No open notice passed the hard checks." in output
    assert "states_served 1" in output
    assert "Next step" not in output


def test_an_empty_database_is_handled(connection):
    result = shortlist(connection)
    assert result.scanned_count == 0
    assert "Scanned 0 open notices" in format_shortlist(result)


TYPE_PREFERENCE = ["Solicitation",
                   "Combined Synopsis/Solicitation", "Sources Sought"]


def make_ranking_profile(**overrides):
    values = {"notice_types": TYPE_PREFERENCE,
              "capabilities": ["cloud migration", "cybersecurity"]}
    values.update(overrides)
    return make_profile(**values)


def test_a_more_preferred_notice_type_ranks_first_even_if_it_is_older(connection):
    store_notice(connection, "sources-sought-newer", notice_type="Sources Sought",
                 posted_date="2026-11-10")
    store_notice(connection, "combined-middle", notice_type="Combined Synopsis/Solicitation",
                 posted_date="2026-11-05")
    store_notice(connection, "solicitation-older", notice_type="Solicitation",
                 posted_date="2026-10-01")
    result = shortlist(connection, make_ranking_profile())
    assert shortlisted_ids(result) == ["solicitation-older", "combined-middle",
                                       "sources-sought-newer"]


def test_more_capability_matches_rank_first_within_the_same_type(connection):
    store_notice(connection, "no-hits", posted_date="2026-11-10")
    store_notice(connection, "one-hit",
                 title="Cloud migration help", posted_date="2026-11-05")
    store_notice(connection, "two-hits", title="Cloud migration and cybersecurity",
                 posted_date="2026-10-01")
    result = shortlist(connection, make_ranking_profile())
    assert shortlisted_ids(result) == ["two-hits", "one-hit", "no-hits"]


def test_capability_phrases_are_also_found_in_the_description(connection):
    store_notice(connection, "plain", posted_date="2026-11-10")
    store_notice(connection, "described", posted_date="2026-10-01",
                 description_text="The agency needs cybersecurity monitoring.")
    result = shortlist(connection, make_ranking_profile())
    assert shortlisted_ids(result) == ["described", "plain"]
    assert result.shortlisted[0].capability_hits == {"cybersecurity": 1}


def test_unknown_fields_still_outrank_the_type_preference(connection):
    store_notice(connection, "preferred-type-but-unknown", state=None,
                 notice_type="Solicitation", title="Cloud migration")
    store_notice(connection, "clean-but-less-preferred",
                 notice_type="Sources Sought")
    result = shortlist(connection, make_ranking_profile(
        notice_types=["Solicitation", "Sources Sought"]))
    assert shortlisted_ids(result) == [
        "clean-but-less-preferred", "preferred-type-but-unknown"]


def test_an_empty_notice_type_list_ranks_every_type_equally(connection):
    store_notice(connection, "older", posted_date="2026-10-01",
                 notice_type="Solicitation")
    store_notice(connection, "newer", posted_date="2026-11-10",
                 notice_type="Sources Sought")
    result = shortlist(connection, make_ranking_profile(notice_types=[]))
    assert shortlisted_ids(result) == ["newer", "older"]


def test_a_notice_with_no_type_ranks_after_one_with_a_known_preferred_type(connection):
    store_notice(connection, "no-type", notice_type=None,
                 posted_date="2026-11-10")
    store_notice(connection, "known-type",
                 notice_type="Sources Sought", posted_date="2026-10-01")
    result = shortlist(connection, make_ranking_profile(
        notice_types=["Solicitation", "Sources Sought"]))
    assert shortlisted_ids(result) == ["known-type", "no-type"]


def test_a_notice_with_no_description_does_not_break_the_ranking(connection):
    store_notice(connection, "no-description",
                 description_text=None, title="Cloud migration")
    result = shortlist(connection, make_ranking_profile())
    assert shortlisted_ids(result) == ["no-description"]
    assert result.shortlisted[0].capability_hits == {"cloud migration": 1}


def test_the_output_shows_each_notices_capability_hits(connection):
    store_notice(connection, "hit", title="Cybersecurity support")
    store_notice(connection, "miss")
    output = format_shortlist(shortlist(connection, make_ranking_profile()))
    assert "capability_hits: cybersecurity" in output
    assert output.count("capability_hits:") == 2

# ------------------------------------------------------------------ output format


def test_the_output_shows_the_fields_the_agent_needs_to_pick_one(connection):
    store_notice(connection, "abc123", title="Cloud Services",
                 days_until_deadline=9)
    output = format_shortlist(shortlist(connection))
    expected_deadline = (TODAY + timedelta(days=9)).isoformat()
    for expected in ("notice_id: abc123", "title: Cloud Services", "type: Solicitation",
                     "naics_code: 541512", "set_aside: SBA", "state: Texas",
                     f"response_deadline: {expected_deadline}", "unknown: none",
                     "Next step: run score_opportunity"):
        assert expected in output
    assert "T17:00:00" not in output   # date only, no time or offset


def test_the_summary_line_reports_all_three_counts(connection):
    store_notice(connection, "pass")
    store_notice(connection, "unknown", state=None)
    store_notice(connection, "fail", state="Ohio")
    output = format_shortlist(shortlist(connection))
    assert ("Scanned 3 open notices. Passed every hard check: 1. "
            "No failures but some unknown fields: 1. Failed at least one check: 1.") in output


# ------------------------------------------------------------------ the tool wrapper

PROFILE_TOML = """\
name = "Test Co"
naics_codes = ["541512"]
set_aside_eligibilities = ["SBA"]
states_served = ["Texas"]
notice_types = ["Solicitation"]
minimum_days_to_respond = 5
"""


@pytest.fixture
def real_files(tmp_path, monkeypatch):
    database_path = tmp_path / "tool.db"
    profile_path = tmp_path / "profile.toml"
    profile_path.write_text(PROFILE_TOML, encoding="utf-8")
    file_database = init_db(database_path)
    # the tool uses the real current date, so deadlines are relative to today
    store_notice(file_database, "tool-good", today=date.today())
    store_notice(file_database, "tool-bad", state="Ohio", today=date.today())
    file_database.close()
    monkeypatch.setattr(tool_module, "settings",
                        SimpleNamespace(db_path=database_path.resolve(), profile_path=profile_path))


def test_the_tool_reads_the_database_and_profile_and_returns_the_shortlist(real_files):
    output = shortlist_opportunities.invoke({})
    assert "tool-good" in output and "tool-bad" not in output
    assert "Failed at least one check: 1." in output


def test_the_tool_accepts_filters(real_files):
    assert "No open notice passed" in shortlist_opportunities.invoke(
        {"keyword": "zzz-no-match"})


def test_the_tool_opens_the_database_read_only(real_files, monkeypatch):
    opened_targets = []
    real_connect = sqlite3.connect

    def recording_connect(target, *args, **kwargs):
        opened_targets.append(target)
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    shortlist_opportunities.invoke({})
    assert len(opened_targets) == 1 and "mode=ro" in opened_targets[0]


# ------------------------------------------------------------------ schema and registration

def test_the_input_defaults_and_limits():
    arguments = ShortlistOpportunitiesInput()
    assert (arguments.naics_code, arguments.keyword,
            arguments.limit) == (None, None, 5)
    for bad_limit in (0, MAX_RESULTS + 1):
        with pytest.raises(ValidationError):
            ShortlistOpportunitiesInput(limit=bad_limit)


def test_the_tool_is_registered_and_read_only_so_it_needs_no_approval():
    assert shortlist_opportunities in LOCAL_TOOLS
    assert ACTION_RISK["shortlist_opportunities"] is RiskLevel.LOW
    assert not needs_human_approval("shortlist_opportunities")
