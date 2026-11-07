import sqlite3

import pytest

from src.ingestion import init_db, upsert
from src.tools.search_opportunities import MAX_RESULTS, format_rows, search_opportunities, search_rows


def make_notice(notice_id, **overrides):
    notice = {"notice_id": notice_id, "title": f"Title {notice_id}", "type": "Solicitation",
              "posted_date": "2026-10-20", "response_deadline": "2026-12-01T10:00:00-05:00",
              "naics_code": "541512", "type_of_set_aside": None, "pop_state": "Texas",
              "description": None, "description_text": None, "ui_link": f"https://sam.gov/{notice_id}"}
    notice.update(overrides)
    return notice


@pytest.fixture
def connection():
    database = init_db(":memory:")
    yield database
    database.close()


def ids(rows):
    return [row[0] for row in rows]


def test_no_filters_returns_everything_newest_first(connection):
    upsert(connection, [make_notice("old", posted_date="2026-10-01"),
                        make_notice("new", posted_date="2026-10-25")])
    assert ids(search_rows(connection)) == ["new", "old"]


def test_keyword_matches_the_title_even_when_description_text_is_missing(connection):
    upsert(connection, [make_notice("a", title="Cloud migration support"),
                        make_notice("b", title="Janitorial services")])
    assert ids(search_rows(connection, keyword="cloud")) == ["a"]


def test_keyword_also_searches_the_description_text(connection):
    upsert(connection, [make_notice("a", description_text="<p>needs Kubernetes experience</p>"),
                        make_notice("b")])
    assert ids(search_rows(connection, keyword="kubernetes")) == ["a"]


def test_state_set_aside_naics_and_date_filters(connection):
    upsert(connection, [
        make_notice("match", pop_state="Texas", type_of_set_aside="SBA",
                    naics_code="541511", posted_date="2026-10-25"),
        make_notice("wrong-state", pop_state="Ohio",
                    type_of_set_aside="SBA", naics_code="541511"),
        make_notice("wrong-set-aside", type_of_set_aside=None,
                    naics_code="541511"),
        make_notice("wrong-naics", type_of_set_aside="SBA",
                    naics_code="999999"),
        make_notice("too-old", type_of_set_aside="SBA",
                    naics_code="541511", posted_date="2026-09-01"),
    ])
    rows = search_rows(connection, state="texas", set_aside="sba", naics_code="541511",
                       posted_after="2026-10-10")
    assert ids(rows) == ["match"]


def test_filters_are_combined_with_and_not_or(connection):
    upsert(connection, [make_notice("only-state", pop_state="Texas", naics_code="111111"),
                        make_notice("only-naics", pop_state="Ohio", naics_code="541512")])
    assert search_rows(connection, state="Texas", naics_code="541512") == []


def test_limit_applies_and_is_capped(connection):
    upsert(connection, [make_notice(f"n{index:03d}")
           for index in range(MAX_RESULTS + 10)])
    assert len(search_rows(connection, limit=3)) == 3
    assert len(search_rows(connection, limit=10_000)) == MAX_RESULTS
    assert len(search_rows(connection, limit=0)) == 1


def test_a_keyword_with_sql_in_it_is_just_text(connection):
    upsert(connection, [make_notice("a")])
    assert search_rows(
        connection, keyword="x'; DROP TABLE solicitations; --") == []
    assert connection.execute(
        "SELECT COUNT(*) FROM solicitations").fetchone()[0] == 1


def test_output_omits_the_long_description_and_empty_fields(connection):
    upsert(connection, [make_notice(
        "a", description_text="LONG TEXT " * 500, pop_state=None)])
    text = format_rows(search_rows(connection))
    assert "LONG TEXT" not in text
    assert "pop_state" not in text
    assert "notice_id: a" in text and "ui_link: https://sam.gov/a" in text


def test_no_results_says_so():
    assert format_rows([]) == "No matching opportunities found."


def test_the_tool_end_to_end_against_a_temporary_database(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.tools import search_opportunities as module
    path = tmp_path / "tool.db"
    writer = init_db(path)
    upsert(writer, [make_notice("a", title="Cloud migration"),
           make_notice("b", title="Janitorial")])
    writer.close()
    monkeypatch.setattr(module, "settings", SimpleNamespace(db_path=path))
    result = search_opportunities.invoke({"keyword": "cloud"})
    assert "notice_id: a" in result and "notice_id: b" not in result


def test_the_tool_opens_the_database_read_only(tmp_path, monkeypatch):
    # a read-only connection refuses to create a missing file; a normal one silently would
    from types import SimpleNamespace
    from src.tools import search_opportunities as module
    path = tmp_path / "does_not_exist.db"
    monkeypatch.setattr(module, "settings", SimpleNamespace(db_path=path))
    with pytest.raises(sqlite3.OperationalError):
        search_opportunities.invoke({})
    assert not path.exists()


def test_the_model_sees_a_description_and_only_optional_arguments():
    schema = search_opportunities.args_schema.model_json_schema()
    assert search_opportunities.description.strip()
    assert "required" not in schema or schema["required"] == []
    for property_schema in schema["properties"].values():
        assert property_schema.get("description")
