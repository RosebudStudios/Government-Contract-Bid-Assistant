import pytest

from src import attachments
from src.attachments import index_chunks, index_pending, set_document_status
from src.ingestion import init_db


@pytest.fixture
def connection():
    database = init_db(":memory:")
    yield database
    database.close()


def add_document(connection, document_id, filename="file.pdf", notice_id="n1", status="downloaded"):
    connection.execute(
        """INSERT INTO documents (id, notice_id, filename, link, status)
           VALUES (:document_id, :notice_id, :filename, :link, :status)""",
        {"document_id": document_id, "filename": filename, "notice_id": notice_id,
         "link": f"https://sam.gov/files/{document_id}", "status": status})
    connection.commit()


def find_chunks(connection, word):
    """Search the way the agent's tool will: a quoted word, ranked by relevance."""
    match_query = '"' + word + '"'
    return connection.execute(
        """SELECT notice_id, document_id, page_number FROM document_chunks
           WHERE document_chunks MATCH :match_query ORDER BY rank""",
        {"match_query": match_query}).fetchall()


def count_chunks(connection):
    return connection.execute("SELECT count(*) FROM document_chunks").fetchone()[0]


def status_of(connection, document_id):
    return connection.execute(
        "SELECT status FROM documents WHERE id = :document_id",
        {"document_id": document_id}).fetchone()[0]


def error_of(connection, document_id):
    return connection.execute(
        "SELECT error FROM documents WHERE id = :document_id",
        {"document_id": document_id}).fetchone()[0]


# ------------------------------------------------------------------ init_db and the chunk table

def test_init_db_creates_the_chunk_table_and_can_run_twice(tmp_path):
    database_path = tmp_path / "chunks.db"
    first = init_db(database_path)
    first.close()
    # CREATE ... IF NOT EXISTS must not fail
    second = init_db(database_path)
    table_names = [row[0] for row in second.execute(
        "SELECT name FROM sqlite_master WHERE name = 'document_chunks'")]
    second.close()
    assert table_names == ["document_chunks"]


# ------------------------------------------------------------------ set_document_status

def test_set_document_status_changes_only_the_named_document(connection):
    add_document(connection, 1, status="downloaded")
    add_document(connection, 2, status="downloaded")
    set_document_status(connection, 1, "indexed")
    assert status_of(connection, 1) == "indexed"
    assert status_of(connection, 2) == "downloaded"


def test_set_document_status_stores_the_error_and_clears_it_when_none_is_given(connection):
    add_document(connection, 1)
    set_document_status(connection, 1, "failed", "boom")
    assert error_of(connection, 1) == "boom"
    set_document_status(connection, 1, "downloaded")
    assert error_of(connection, 1) is None


def test_set_document_status_is_saved_to_disk(tmp_path):
    database_path = tmp_path / "status.db"
    first = init_db(database_path)
    add_document(first, 1)
    set_document_status(first, 1, "indexed")
    first.close()                             # a missing commit() loses the change here
    second = init_db(database_path)
    saved_status = status_of(second, 1)
    second.close()
    assert saved_status == "indexed"


# ------------------------------------------------------------------ index_chunks

def test_an_indexed_chunk_can_be_found_with_its_notice_document_and_page(connection):
    index_chunks(connection, 7, "abc", [
                 (1, "sprinkler systems"), (2, "fire alarm panel")])
    assert find_chunks(connection, "alarm") == [("abc", 7, 2)]


def test_indexing_the_same_document_twice_does_not_duplicate_rows(connection):
    chunks = [(1, "sprinkler systems"), (2, "fire alarm panel")]
    index_chunks(connection, 7, "abc", chunks)
    index_chunks(connection, 7, "abc", chunks)
    assert count_chunks(connection) == 2


def test_reindexing_replaces_the_old_text(connection):
    index_chunks(connection, 7, "abc", [(1, "asbestos abatement")])
    index_chunks(connection, 7, "abc", [(1, "roof repair")])
    assert find_chunks(connection, "asbestos") == []
    assert find_chunks(connection, "roof") == [("abc", 7, 1)]


def test_reindexing_one_document_leaves_another_alone(connection):
    index_chunks(connection, 1, "n1", [(1, "sprinkler systems")])
    index_chunks(connection, 2, "n2", [(1, "fire alarm panel")])
    index_chunks(connection, 1, "n1", [(1, "roof repair")])
    assert find_chunks(connection, "alarm") == [("n2", 2, 1)]


def test_indexing_an_empty_list_just_clears_the_documents_old_rows(connection):
    index_chunks(connection, 7, "abc", [(1, "sprinkler systems")])
    index_chunks(connection, 7, "abc", [])
    assert count_chunks(connection) == 0


def test_stemming_matches_other_forms_of_a_word(connection):
    index_chunks(connection, 7, "abc", [
                 (1, "quarterly inspections are required")])
    assert find_chunks(connection, "inspection") == [("abc", 7, 1)]


def test_a_word_that_is_not_in_any_chunk_finds_nothing(connection):
    index_chunks(connection, 7, "abc", [(1, "sprinkler systems")])
    assert find_chunks(connection, "elevator") == []


def test_indexed_chunks_are_saved_to_disk(tmp_path):
    database_path = tmp_path / "chunks.db"
    first = init_db(database_path)
    index_chunks(first, 7, "abc", [(1, "sprinkler systems")])
    first.close()
    second = init_db(database_path)
    found = find_chunks(second, "sprinkler")
    second.close()
    assert found == [("abc", 7, 1)]


# ------------------------------------------------------------------ index_pending

def fake_pages(pdf_path):
    return [(1, "The contractor shall maintain the sprinkler system."),
            (3, "Quarterly fire alarm testing is required.")]


def test_a_downloaded_document_is_chunked_indexed_and_marked_indexed(connection, tmp_path, monkeypatch):
    add_document(connection, 1, notice_id="n1")
    monkeypatch.setattr(attachments, "extract_pages", fake_pages)
    index_pending(connection, tmp_path)
    assert status_of(connection, 1) == "indexed"
    assert find_chunks(connection, "sprinkler") == [("n1", 1, 1)]
    assert find_chunks(connection, "alarm") == [("n1", 1, 3)]


def test_the_file_that_is_read_is_the_document_id_pdf_in_the_folder(connection, tmp_path, monkeypatch):
    add_document(connection, 42)
    paths_read = []

    def recording_extract_pages(pdf_path):
        paths_read.append(pdf_path)
        return []

    monkeypatch.setattr(attachments, "extract_pages", recording_extract_pages)
    # a plain string folder must work too
    index_pending(connection, str(tmp_path))
    assert paths_read == [tmp_path / "42.pdf"]


def test_a_document_with_no_extractable_text_is_marked_no_text(connection, tmp_path, monkeypatch):
    add_document(connection, 1)
    monkeypatch.setattr(attachments, "extract_pages", lambda pdf_path: [])
    index_pending(connection, tmp_path)
    assert status_of(connection, 1) == "no_text"
    assert count_chunks(connection) == 0


def test_a_missing_file_is_marked_failed_with_an_error(connection, tmp_path):
    add_document(connection, 1)                   # no 1.pdf exists in tmp_path
    index_pending(connection, tmp_path)
    assert status_of(connection, 1) == "failed"
    assert error_of(connection, 1)


def test_a_file_that_is_not_really_a_pdf_is_marked_failed(connection, tmp_path):
    add_document(connection, 1)
    (tmp_path / "1.pdf").write_bytes(b"this is not a pdf")
    index_pending(connection, tmp_path)
    assert status_of(connection, 1) == "failed"
    assert error_of(connection, 1)


def test_a_failing_document_does_not_stop_the_rest(connection, tmp_path, monkeypatch):
    add_document(connection, 1)
    add_document(connection, 2)

    def sometimes_failing_extract_pages(pdf_path):
        if pdf_path.name == "1.pdf":
            raise ValueError("corrupt file")
        return [(1, "sprinkler system installation")]

    monkeypatch.setattr(attachments, "extract_pages",
                        sometimes_failing_extract_pages)
    index_pending(connection, tmp_path)
    assert status_of(connection, 1) == "failed"
    assert "corrupt file" in error_of(connection, 1)
    assert status_of(connection, 2) == "indexed"


def test_only_downloaded_documents_are_processed(connection, tmp_path, monkeypatch):
    add_document(connection, 1, status="pending")
    add_document(connection, 2, status="indexed")
    add_document(connection, 3, status="failed")
    add_document(connection, 4, status="unsupported")

    def must_not_be_called(pdf_path):
        raise AssertionError("extract_pages should not have been called")

    monkeypatch.setattr(attachments, "extract_pages", must_not_be_called)
    index_pending(connection, tmp_path)
    statuses = [status_of(connection, document_id)
                for document_id in (1, 2, 3, 4)]
    assert statuses == ["pending", "indexed", "failed", "unsupported"]


def test_running_it_twice_does_not_index_a_document_twice(connection, tmp_path, monkeypatch):
    add_document(connection, 1)
    monkeypatch.setattr(attachments, "extract_pages", fake_pages)
    index_pending(connection, tmp_path)
    chunk_count_after_first_run = count_chunks(connection)
    index_pending(connection, tmp_path)
    assert count_chunks(connection) == chunk_count_after_first_run
