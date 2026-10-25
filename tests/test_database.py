import json
import sqlite3
from pathlib import Path

import pytest

from src.ingestion import init_db, normalize, upsert

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_response.json"


@pytest.fixture(scope="module")
def records():
    with open(FIXTURE, encoding="utf-8") as f:
        rows = json.load(f)["opportunitiesData"]
    return [normalize(row) for row in rows]


@pytest.fixture
def con():
    # a fresh in-memory database for every test, so tests can't affect each other
    connection = init_db(":memory:")
    yield connection
    connection.close()


def count(con):
    return con.execute("SELECT COUNT(*) FROM solicitations").fetchone()[0]


def test_empty_list_returns_zero_and_writes_nothing(con):
    assert upsert(con, []) == 0
    assert count(con) == 0


def test_returns_number_of_records_and_stores_them_all(con, records):
    assert upsert(con, records) == len(records)
    assert count(con) == len(records)


def test_running_twice_does_not_duplicate(con, records):
    upsert(con, records)
    upsert(con, records)
    assert count(con) == len(records)


def test_changed_record_updates_in_place(con, records):
    upsert(con, records)
    changed = {**records[0], "title": "CHANGED TITLE",
               "response_deadline": "2030-01-01"}
    upsert(con, [changed])
    assert count(con) == len(records)
    row = con.execute(
        "SELECT title, response_deadline FROM solicitations WHERE notice_id = ?",
        (changed["notice_id"],),
    ).fetchone()
    assert row == ("CHANGED TITLE", "2030-01-01")


def test_other_rows_are_untouched_by_an_update(con, records):
    upsert(con, records)
    upsert(con, [{**records[0], "title": "CHANGED TITLE"}])
    other = records[1]
    title = con.execute(
        "SELECT title FROM solicitations WHERE notice_id = ?", (
            other["notice_id"],)
    ).fetchone()[0]
    assert title == other["title"]


def test_stored_row_matches_the_record(con, records):
    upsert(con, records)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM solicitations WHERE notice_id = ?", (
            records[0]["notice_id"],)
    ).fetchone()
    assert dict(row) == records[0]


def test_none_is_stored_as_sql_null(con, records):
    upsert(con, records)
    expected = sum(rec["naics_code"] is None for rec in records)
    actual = con.execute(
        "SELECT COUNT(*) FROM solicitations WHERE naics_code IS NULL"
    ).fetchone()[0]
    assert actual == expected


def test_query_by_naics_code(con, records):
    upsert(con, records)
    code = next(rec["naics_code"] for rec in records if rec["naics_code"])
    expected = sum(rec["naics_code"] == code for rec in records)
    rows = con.execute(
        "SELECT notice_id FROM solicitations WHERE naics_code = ?", (code,)
    ).fetchall()
    assert len(rows) == expected >= 1


def test_resource_links_round_trip_as_json(con, records):
    upsert(con, records)
    with_links = next(rec for rec in records if rec["resource_links"])
    stored = con.execute(
        "SELECT resource_links FROM solicitations WHERE notice_id = ?",
        (with_links["notice_id"],),
    ).fetchone()[0]
    assert isinstance(json.loads(stored), list)


def test_missing_notice_id_is_rejected(con, records):
    bad = {**records[0], "notice_id": None}
    with pytest.raises(sqlite3.IntegrityError):
        upsert(con, [bad])


def test_data_survives_closing_and_reopening(tmp_path, records):
    path = tmp_path / "test.db"
    first = init_db(path)
    upsert(first, records)
    first.close()
    # init_db must be safe to call on an existing database
    second = init_db(path)
    assert count(second) == len(records)
    second.close()
