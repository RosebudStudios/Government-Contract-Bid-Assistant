import json
from pathlib import Path

import pytest

from src.ingestion import normalize

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_response.json"

EXPECTED_KEYS = {
    "notice_id", "title", "solicitation_number", "type", "posted_date",
    "response_deadline", "naics_code", "classification_code",
    "type_of_set_aside", "type_of_set_aside_description",
    "full_parent_path_name", "ui_link", "description",
    "pop_city", "pop_state", "office_city", "office_state",
    "contact_name", "contact_email", "resource_links", "active",
    "archive_date"
}


@pytest.fixture(scope="module")
def raw_rows():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["opportunitiesData"]


@pytest.fixture(scope="module")
def normalized(raw_rows):
    return [normalize(row) for row in raw_rows]


def test_one_output_per_row(raw_rows, normalized):
    assert len(normalized) == len(raw_rows) > 0


def test_every_record_has_exactly_the_expected_keys(normalized):
    for rec in normalized:
        assert set(rec) == EXPECTED_KEYS


def test_notice_ids_are_unique_and_present(normalized):
    ids = [rec["notice_id"] for rec in normalized]
    assert all(ids)
    assert len(set(ids)) == len(ids)


def test_no_nested_values(normalized):
    # SQLite columns hold scalars, so dicts/lists must be flattened or JSON text
    for rec in normalized:
        for key, value in rec.items():
            assert not isinstance(
                value, (dict, list)), f"{key} is {type(value).__name__}"


def test_response_deadline_matches_raw(raw_rows, normalized):
    # catches a misspelled key: .get() on a wrong name silently returns None
    # raw data sometimes has "" instead of null; normalize turns both into None
    for raw, rec in zip(raw_rows, normalized):
        assert rec["response_deadline"] == (
            raw.get("responseDeadLine") or None)


def test_naics_code_matches_raw(raw_rows, normalized):
    for raw, rec in zip(raw_rows, normalized):
        assert rec["naics_code"] == raw.get("naicsCode")


def test_placeholder_strings_become_none(normalized):
    placeholders = {"", "none", "n/a"}
    for rec in normalized:
        for key, value in rec.items():
            if isinstance(value, str):
                assert value.strip().lower() not in placeholders, key


def test_real_values_survive_cleanup(normalized):
    # guards against a cleanup loop that wipes everything
    assert sum(rec["title"] is not None for rec in normalized) == len(
        normalized)
    assert any(rec["type_of_set_aside"] == "SBA" for rec in normalized)


def test_contact_comes_from_first_point_of_contact(raw_rows, normalized):
    for raw, rec in zip(raw_rows, normalized):
        contacts = raw.get("pointOfContact") or []
        expected = contacts[0].get("email") if contacts else None
        assert rec["contact_email"] == expected


def test_resource_links_is_none_or_json_list(raw_rows, normalized):
    for raw, rec in zip(raw_rows, normalized):
        if raw.get("resourceLinks"):
            assert json.loads(rec["resource_links"]) == raw["resourceLinks"]
        else:
            assert rec["resource_links"] is None


def test_minimal_row_does_not_crash():
    rec = normalize({"noticeId": "x"})
    assert rec["notice_id"] == "x"
    # every other field must come back as a real None, not {}, [] or "null"
    for key, value in rec.items():
        if key != "notice_id":
            assert value is None, f"{key} = {value!r}"
