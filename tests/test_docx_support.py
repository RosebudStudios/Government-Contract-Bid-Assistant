import asyncio

import docx
import httpx
import pytest

from src import attachments
from src.attachments import extract_pages, index_chunks, index_pending
from src.ingestion import SamClient, init_db


@pytest.fixture
def connection():
    database = init_db(":memory:")
    yield database
    database.close()


def make_word_file(path, paragraphs=(), table_rows=()):
    """Build a genuine .docx on disk: paragraphs first, then an optional table."""
    word_document = docx.Document()
    for paragraph_text in paragraphs:
        word_document.add_paragraph(paragraph_text)
    if table_rows:
        table = word_document.add_table(
            rows=len(table_rows), cols=len(table_rows[0]))
        for row_index, row_values in enumerate(table_rows):
            for column_index, cell_value in enumerate(row_values):
                table.cell(row_index, column_index).text = cell_value
    word_document.save(path)


def add_document(connection, document_id, filename, status="downloaded", notice_id="n1"):
    connection.execute(
        """INSERT INTO documents (id, notice_id, filename, link, status)
           VALUES (:document_id, :notice_id, :filename, :link, :status)""",
        {"document_id": document_id, "notice_id": notice_id, "filename": filename,
         "link": f"https://sam.gov/files/{document_id}", "status": status})
    connection.commit()


def find_chunks(connection, word):
    match_query = '"' + word + '"'
    return connection.execute(
        """SELECT notice_id, document_id, page_number FROM document_chunks
           WHERE document_chunks MATCH :match_query ORDER BY rank""",
        {"match_query": match_query}).fetchall()


def status_and_error(connection, document_id):
    return connection.execute(
        "SELECT status, error FROM documents WHERE id = :document_id",
        {"document_id": document_id}).fetchone()


# ------------------------------------------------------------------ extract_pages for Word files

def test_word_paragraphs_come_back_as_one_page_with_no_page_number(tmp_path):
    word_path = tmp_path / "1.docx"
    make_word_file(word_path, paragraphs=[
                   "Scope of work", "The contractor shall inspect sprinklers."])
    pages = extract_pages(word_path)
    assert len(pages) == 1
    page_number, page_text = pages[0]
    assert page_number is None
    assert "The contractor shall inspect sprinklers." in page_text


def test_word_table_text_is_included(tmp_path):
    word_path = tmp_path / "1.docx"
    make_word_file(word_path, paragraphs=["Deliverables"],
                   table_rows=[("Item", "Due"), ("Quarterly report", "Every quarter")])
    page_text = extract_pages(word_path)[0][1]
    assert "Quarterly report | Every quarter" in page_text


def test_a_word_file_with_only_a_table_still_has_text(tmp_path):
    word_path = tmp_path / "1.docx"
    make_word_file(word_path, table_rows=[("Labor category", "Rate")])
    assert extract_pages(word_path) != []


def test_an_empty_word_file_gives_no_pages(tmp_path):
    word_path = tmp_path / "1.docx"
    make_word_file(word_path)
    assert extract_pages(word_path) == []


def test_the_word_extension_is_matched_in_any_letter_case(tmp_path):
    word_path = tmp_path / "1.DOCX"
    make_word_file(word_path, paragraphs=["hello there"])
    assert extract_pages(word_path)[0][1] == "hello there"


def test_a_file_with_a_docx_extension_that_is_not_word_raises(tmp_path):
    word_path = tmp_path / "1.docx"
    word_path.write_bytes(b"this is not a word file")
    with pytest.raises(Exception):
        extract_pages(word_path)


# ------------------------------------------------------------------ downloading Word files

def run_download(attachment_filename, tmp_path):
    """Download one attachment from a fake SAM whose redirect names the given file."""
    def handler(request):
        if request.url.host == "sam.gov":
            return httpx.Response(303, headers={
                "content-disposition": f'attachment; filename="{attachment_filename}"',
                "content-type": "application/octet-stream",
                "location": "https://files.example.com/signed-url"})
        return httpx.Response(200, content=b"file bytes")

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        samclient = SamClient(api_key="test-key", client=client)
        try:
            return await samclient.download_attachment(
                "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/abc/download",
                tmp_path, "5", max_bytes=1000)
        finally:
            await samclient.aclose()

    return asyncio.run(go())


def test_a_word_attachment_is_downloaded_not_skipped(tmp_path):
    result = run_download("Statement of Work.docx", tmp_path)
    assert result.status == "downloaded"
    assert result.saved_path == tmp_path / "5.docx"
    assert (tmp_path / "5.docx").read_bytes() == b"file bytes"


def test_an_uppercase_extension_is_saved_in_lowercase(tmp_path):
    result = run_download("SOW.DOCX", tmp_path)
    assert result.saved_path == tmp_path / "5.docx"


def test_a_pdf_attachment_is_still_downloaded(tmp_path):
    assert run_download("Plan.pdf", tmp_path).status == "downloaded"


def test_an_old_style_word_file_is_still_unsupported(tmp_path):
    result = run_download("Old Form.doc", tmp_path)
    assert result.status == "unsupported"
    assert list(tmp_path.iterdir()) == []         # nothing was saved


def test_a_spreadsheet_is_still_unsupported(tmp_path):
    assert run_download("Rates.xlsx", tmp_path).status == "unsupported"


# ------------------------------------------------------------------ indexing Word files

def test_a_downloaded_word_file_is_indexed_and_found_with_no_page_number(connection, tmp_path):
    add_document(connection, 7, "Statement of Work.docx", notice_id="n7")
    make_word_file(tmp_path / "7.docx", paragraphs=["Maintenance requirements"],
                   table_rows=[("Report", "Frequency"), ("Inspection", "Quarterly")])
    assert index_pending(connection, tmp_path) == 1
    assert status_and_error(connection, 7) == ("indexed", None)
    assert find_chunks(connection, "quarterly") == [("n7", 7, None)]


def test_the_file_read_uses_the_extension_from_the_stored_filename(connection, tmp_path, monkeypatch):
    add_document(connection, 3, "Report.DOCX")
    add_document(connection, 4, "Plan.pdf")
    paths_read = []

    def recording_extract_pages(file_path):
        paths_read.append(file_path)
        return []

    monkeypatch.setattr(attachments, "extract_pages", recording_extract_pages)
    index_pending(connection, str(tmp_path))
    assert paths_read == [tmp_path / "3.docx", tmp_path / "4.pdf"]


def test_a_downloaded_pdf_is_not_marked_no_text_just_because_of_its_extension(connection, tmp_path, monkeypatch):
    add_document(connection, 1, "Plan.pdf")
    monkeypatch.setattr(attachments, "extract_pages",
                        lambda file_path: [(1, "sprinkler system")] if file_path.suffix == ".pdf" else [])
    index_pending(connection, tmp_path)
    assert status_and_error(connection, 1)[0] == "indexed"


def test_a_document_with_no_stored_filename_is_marked_failed(connection, tmp_path):
    add_document(connection, 1, None)
    # the next document must still be processed
    add_document(connection, 2, "Plan.pdf")
    index_pending(connection, tmp_path)
    status, error = status_and_error(connection, 1)
    assert status == "failed" and error
    # no such file: failed, but it was tried
    assert status_and_error(connection, 2)[0] == "failed"


def test_a_page_number_of_none_is_stored_and_returned_as_none(connection):
    index_chunks(connection, 5, "abc", [
                 (None, "word files carry no numbering"), (2, "pdf leaflet two")])
    assert find_chunks(connection, "numbering") == [("abc", 5, None)]
    assert find_chunks(connection, "leaflet") == [("abc", 5, 2)]
