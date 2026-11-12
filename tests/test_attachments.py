import json
import sqlite3

import pytest

from src import attachments
from src.attachments import fixed_chunk_text, download_pending, extract_pages, register_documents
from src.ingestion import DownloadResult, RateLimitError, init_db, upsert

TODAY_TEXT = "2026-11-10"


def make_notice(notice_id, links, **overrides):
    notice = {"notice_id": notice_id, "title": "T", "naics_code": "541512",
              "response_deadline": "2026-12-01T10:00:00-05:00",
              "resource_links": json.dumps(links) if links is not None else None}
    notice.update(overrides)
    return notice


@pytest.fixture
def connection():
    database = init_db(":memory:")
    yield database
    database.close()


def documents(connection):
    return connection.execute(
        "SELECT notice_id, link, status FROM documents ORDER BY id").fetchall()


# ------------------------------------------------------------------ register_documents

def test_every_link_of_a_matching_notice_becomes_a_pending_document(connection):
    upsert(connection, [make_notice(
        "n1", ["https://sam.gov/a", "https://sam.gov/b"])])
    assert register_documents(connection, TODAY_TEXT, ("541512",)) == 2
    assert documents(connection) == [("n1", "https://sam.gov/a", "pending"),
                                     ("n1", "https://sam.gov/b", "pending")]


def test_running_it_twice_adds_nothing_and_keeps_existing_statuses(connection):
    upsert(connection, [make_notice("n1", ["https://sam.gov/a"])])
    register_documents(connection, TODAY_TEXT, ())
    connection.execute("UPDATE documents SET status = 'downloaded'")
    assert register_documents(connection, TODAY_TEXT, ()) == 0
    assert documents(connection) == [("n1", "https://sam.gov/a", "downloaded")]


def test_notices_that_do_not_qualify_are_skipped(connection):
    upsert(connection, [
        make_notice("no-links", None),
        make_notice("expired", ["https://sam.gov/x"],
                    response_deadline="2026-11-01T10:00:00-05:00"),
        make_notice("other-naics", ["https://sam.gov/y"], naics_code="999999"),
        make_notice("ok", ["https://sam.gov/z"])])
    register_documents(connection, TODAY_TEXT, ("541512",))
    assert [row[0] for row in documents(connection)] == ["ok"]


def test_with_no_target_codes_every_open_notice_qualifies(connection):
    upsert(connection, [make_notice("a", ["https://sam.gov/1"], naics_code="111111"),
                        make_notice("b", ["https://sam.gov/2"], naics_code="222222")])
    register_documents(connection, TODAY_TEXT, ())
    assert len(documents(connection)) == 2


def test_three_target_codes_work_too(connection):
    upsert(connection, [make_notice(code, [f"https://sam.gov/{code}"], naics_code=code)
                        for code in ("111111", "222222", "333333", "444444")])
    register_documents(connection, TODAY_TEXT, ("111111", "222222", "333333"))
    assert sorted(row[0] for row in documents(connection)) == [
        "111111", "222222", "333333"]


def test_registered_documents_survive_closing_and_reopening(tmp_path):
    database_path = tmp_path / "docs.db"
    first = init_db(database_path)
    upsert(first, [make_notice("n1", ["https://sam.gov/a"])])
    register_documents(first, TODAY_TEXT, ())
    first.close()
    second = init_db(database_path)
    assert len(documents(second)) == 1
    second.close()


# ------------------------------------------------------------------ download_pending

class FakeSamClient:
    """Stands in for SamClient: returns or raises whatever each link is told to."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    async def download_attachment(self, link, folder, file_stem, *, max_bytes, counts_toward_quota=False):
        self.calls.append((link, file_stem, max_bytes))
        outcome = self.outcomes.get(link, DownloadResult(
            "downloaded", "f.pdf", "application/pdf"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def add_pending(connection, links, notice_id="n1"):
    for link in links:
        connection.execute("INSERT INTO documents (notice_id, link) VALUES (:n, :l)",
                           {"n": notice_id, "l": link})
    connection.commit()


def run_downloads(fake, connection, tmp_path, **overrides):
    import asyncio
    options = {"max_downloads": 10, "max_bytes": 1000, "pause_seconds": 0}
    options.update(overrides)
    return asyncio.run(download_pending(fake, connection, tmp_path, **options))


def statuses(connection):
    return [row[0] for row in connection.execute("SELECT status FROM documents ORDER BY id")]


def test_each_result_status_is_stored_with_filename_and_type(connection, tmp_path):
    add_pending(connection, ["a", "b"])
    fake = FakeSamClient({"a": DownloadResult("downloaded", "A.pdf", "application/pdf"),
                          "b": DownloadResult("unsupported", "B.xlsx", "application/vnd.ms-excel")})
    assert run_downloads(fake, connection, tmp_path) == 2
    rows = connection.execute(
        "SELECT status, filename, content_type FROM documents ORDER BY id").fetchall()
    assert rows == [("downloaded", "A.pdf", "application/pdf"),
                    ("unsupported", "B.xlsx", "application/vnd.ms-excel")]


def test_the_document_id_is_used_as_the_file_name_stem(connection, tmp_path):
    add_pending(connection, ["a"])
    fake = FakeSamClient({})
    run_downloads(fake, connection, tmp_path)
    document_id = connection.execute("SELECT id FROM documents").fetchone()[0]
    assert fake.calls[0][1] == str(document_id)


def test_a_failed_download_is_marked_failed_and_the_run_continues(connection, tmp_path):
    add_pending(connection, ["a", "b"])
    fake = FakeSamClient({"a": RuntimeError("file host answered 403")})
    run_downloads(fake, connection, tmp_path)
    assert statuses(connection) == ["failed", "downloaded"]
    assert connection.execute("SELECT error FROM documents WHERE link = 'a'").fetchone()[
        0] == "file host answered 403"


def test_a_rate_limit_stops_the_run_and_leaves_the_rest_pending(connection, tmp_path):
    add_pending(connection, ["a", "b", "c"])
    fake = FakeSamClient({"b": RateLimitError("429")})
    assert run_downloads(fake, connection, tmp_path) == 1
    assert statuses(connection) == ["downloaded", "pending", "pending"]
    assert [call[0] for call in fake.calls] == ["a", "b"]


def test_only_pending_documents_are_processed_and_the_cap_applies(connection, tmp_path):
    add_pending(connection, ["a", "b", "c", "d"])
    connection.execute(
        "UPDATE documents SET status = 'downloaded' WHERE link = 'a'")
    fake = FakeSamClient({})
    run_downloads(fake, connection, tmp_path, max_downloads=2)
    assert [call[0] for call in fake.calls] == ["b", "c"]
    assert statuses(connection) == [
        "downloaded", "downloaded", "downloaded", "pending"]


def test_the_size_cap_is_passed_on(connection, tmp_path):
    add_pending(connection, ["a"])
    fake = FakeSamClient({})
    run_downloads(fake, connection, tmp_path, max_bytes=1234)
    assert fake.calls[0][2] == 1234


def test_statuses_survive_closing_and_reopening(tmp_path):
    database_path = tmp_path / "docs.db"
    first = init_db(database_path)
    add_pending(first, ["a"])
    run_downloads(FakeSamClient({}), first, tmp_path)
    first.close()
    second = init_db(database_path)
    assert statuses(second) == ["downloaded"]
    second.close()


# ------------------------------------------------------------------ extract_pages / fixed_chunk_pages

class FakeLoadedPage:
    def __init__(self, text, page):
        self.page_content = text
        self.metadata = {"page": page}


def test_a_text_file_is_one_page(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello there", encoding="utf-8")
    assert extract_pages(path) == [(1, "hello there")]


def test_pdf_pages_keep_one_based_page_numbers_and_empty_pages_are_dropped(tmp_path, monkeypatch):
    class FakeLoader:
        def __init__(self, path):
            pass

        def load(self):
            return [FakeLoadedPage("first", 0), FakeLoadedPage("   ", 1), FakeLoadedPage("third", 2)]
    monkeypatch.setattr(attachments, "PyPDFLoader", FakeLoader)
    assert extract_pages(tmp_path / "doc.PDF") == [(1, "first"), (3, "third")]


def test_an_unsupported_file_gives_no_pages(tmp_path):
    assert extract_pages(tmp_path / "sheet.xlsx") == []


def test_chunks_keep_their_page_number_and_overlap():
    long_page = "The contractor shall provide cloud migration support. " * 60
    chunks = fixed_chunk_text([(4, "short page"), (7, long_page)],
                              chunk_size=1000, overlap=200)
    assert chunks[0] == (4, "short page")
    long_chunks = [chunk for page, chunk in chunks if page == 7]
    assert len(long_chunks) > 1 and all(
        len(chunk) <= 1000 for chunk in long_chunks)
    assert {page for page, _ in chunks} == {4, 7}
