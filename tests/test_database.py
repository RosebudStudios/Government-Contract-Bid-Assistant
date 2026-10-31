import json
import sqlite3
from pathlib import Path

import pytest

from src.ingestion import (get_notices_missing_description, init_db, normalize,
                           save_description_text, upsert)

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
    assert dict(row) == {**records[0], "description_text": None}


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


def test_upsert_keeps_description_text():
    connection = init_db(":memory:")
    first_record = normalize({"noticeId": "n1", "title": "Old title"})
    upsert(connection, [first_record])
    connection.execute(
        "UPDATE solicitations SET description_text = :description_text WHERE notice_id = :notice_id",
        {"description_text": "<p>full text</p>", "notice_id": "n1"})
    connection.commit()
    changed_record = normalize({"noticeId": "n1", "title": "New title"})
    upsert(connection, [changed_record])
    title, description_text = connection.execute(
        "SELECT title, description_text FROM solicitations WHERE notice_id = 'n1'").fetchone()
    assert title == "New title"
    assert description_text == "<p>full text</p>"


def test_description_text_survives_migration(tmp_path, records):
    path = tmp_path / "test.db"
    first = init_db(path)
    upsert(first, records)
    first.close()
    # init_db must be safe to call on an existing database
    second = init_db(path)
    existing_column_names = [column_row[1] for column_row in second.execute(
        "PRAGMA table_info(solicitations)")]
    assert "description_text" in existing_column_names
    second.close()


def make_notice(notice_id, *, posted_date="2026-10-20", response_deadline="2026-11-05T12:00:00-05:00",
                description=None):
    if description is None:
        description = f"https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid={notice_id}"
    return {"notice_id": notice_id, "title": "T", "posted_date": posted_date,
            "response_deadline": response_deadline, "description": description}


TODAY_TEXT = "2026-10-29"


def missing_ids(connection, fetch_limit=100):
    return [row[0] for row in get_notices_missing_description(connection, TODAY_TEXT, fetch_limit)]


def test_missing_description_returns_id_and_url(con):
    upsert(con, [make_notice("n1")])
    assert get_notices_missing_description(con, TODAY_TEXT, 10) == [
        ("n1", "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=n1")]


def test_notice_with_text_is_not_returned(con):
    upsert(con, [make_notice("n1")])
    save_description_text(con, "n1", "<p>text</p>")
    assert missing_ids(con) == []


def test_empty_string_counts_as_already_fetched(con):
    upsert(con, [make_notice("n1")])
    save_description_text(con, "n1", "")
    assert missing_ids(con) == []


def test_expired_notice_is_skipped(con):
    upsert(con, [make_notice("old", response_deadline="2026-10-28T12:00:00-05:00"),
                 make_notice("open", response_deadline="2026-10-29T08:00:00-05:00")])
    assert missing_ids(con) == ["open"]


def test_notice_without_a_deadline_is_skipped(con):
    upsert(con, [make_notice("n1", response_deadline=None)])
    assert missing_ids(con) == []


def test_notice_whose_description_is_not_a_url_is_skipped(con):
    upsert(con, [make_notice("n1", description="plain text, not a link")])
    assert missing_ids(con) == []


def test_newest_first_and_limit_applies(con):
    upsert(con, [make_notice("a", posted_date="2026-10-01"),
                 make_notice("b", posted_date="2026-10-25"),
                 make_notice("c", posted_date="2026-10-15")])
    assert missing_ids(con) == ["b", "c", "a"]
    assert missing_ids(con, fetch_limit=2) == ["b", "c"]


def test_save_description_text_keeps_the_url_and_other_rows(con):
    upsert(con, [make_notice("n1"), make_notice("n2")])
    updated_row_count = save_description_text(con, "n1", "<p>text</p>")
    assert updated_row_count == 1
    url, text = con.execute(
        "SELECT description, description_text FROM solicitations WHERE notice_id = 'n1'").fetchone()
    assert url.endswith("noticeid=n1") and text == "<p>text</p>"
    assert con.execute(
        "SELECT description_text FROM solicitations WHERE notice_id = 'n2'").fetchone()[0] is None


def test_save_description_text_for_unknown_notice_changes_nothing(con):
    assert save_description_text(con, "missing", "x") == 0


def test_saved_description_text_survives_closing_and_reopening(tmp_path):
    path = tmp_path / "text.db"
    first = init_db(path)
    upsert(first, [make_notice("n1")])
    save_description_text(first, "n1", "<p>kept</p>")
    first.close()
    second = init_db(path)
    assert second.execute("SELECT description_text FROM solicitations").fetchone()[
        0] == "<p>kept</p>"
    second.close()


def test_old_database_without_the_column_gets_it_added(tmp_path):
    path = tmp_path / "old.db"
    old_connection = sqlite3.connect(path)
    old_connection.execute(
        "CREATE TABLE solicitations(notice_id TEXT PRIMARY KEY NOT NULL, title TEXT)")
    old_connection.execute("INSERT INTO solicitations VALUES ('n1', 'Kept')")
    old_connection.commit()
    old_connection.close()
    migrated = init_db(path)
    column_names = [column_row[1] for column_row in migrated.execute(
        "PRAGMA table_info(solicitations)")]
    assert "description_text" in column_names
    assert migrated.execute("SELECT title FROM solicitations").fetchone()[
        0] == "Kept"
    migrated.close()
    init_db(path).close()
