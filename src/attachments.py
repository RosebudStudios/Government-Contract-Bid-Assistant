import json
import asyncio
import docx
from pathlib import Path
from datetime import date
from .ingestion import SamClient, init_db, settings, RateLimitError
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter


def register_documents(connection, today_text, target_naics_codes):
    naics_parameters = {f"naics_{index}": naics_code for index,
                        naics_code in enumerate(target_naics_codes)}
    naics_placeholders = ", ".join(f":{name}" for name in naics_parameters)
    if naics_placeholders:
        naics_filter = f"AND naics_code IN ({naics_placeholders})"
    else:
        naics_filter = ""
    rows = connection.execute(
        f"""SELECT notice_id, resource_links FROM solicitations
        WHERE resource_links IS NOT NULL
        AND substr(response_deadline, 1, 10) >= :today_text
        {naics_filter}""",
        {"today_text": today_text, **naics_parameters}).fetchall()
    document_rows = []
    for notice_id, resource_links_text in rows:
        for link in json.loads(resource_links_text):
            document_rows.append({"notice_id": notice_id, "link": link})
    cursor = connection.executemany(
        """INSERT OR IGNORE INTO documents (notice_id, link) VALUES (:notice_id, :link)""", document_rows)
    connection.commit()
    return cursor.rowcount


def set_document_status(connection, document_id, status, error=None):
    connection.execute("""UPDATE documents 
                        SET status = :status, error = :error 
                        WHERE id = :document_id""", {
        "document_id": document_id, "status": status, "error": error})
    connection.commit()
    return


async def download_pending(samclient, connection, folder, *, max_downloads, max_bytes, counts_toward_quota=False, pause_seconds=1):
    pending_documents = connection.execute(
        """SELECT id, link FROM documents WHERE status = 'pending'
        ORDER BY id LIMIT :max_downloads""", {"max_downloads": max_downloads}
    ).fetchall()
    processed_count = 0
    for document_id, link in pending_documents:
        try:
            result = await samclient.download_attachment(link, folder, str(document_id), max_bytes=max_bytes, counts_toward_quota=counts_toward_quota)
        except RateLimitError:
            print(
                "SAM.gov rate limit reached; stopping. Remaining documents stay pending.")
            break
        except RuntimeError as download_error:
            set_document_status(connection, document_id,
                                "failed", str(download_error))
        else:
            set_document_status(connection, document_id, result.status)
            connection.execute(
                """UPDATE documents SET  filename = :filename, content_type = :content_type WHERE id = :document_id""",
                {"filename": result.filename, "content_type": result.content_type, "document_id": document_id})
        connection.commit()
        processed_count += 1
        await asyncio.sleep(pause_seconds)
    return processed_count


def extract_pages(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".txt":
        pages = [(1, path.read_text(encoding="utf-8", errors="replace"))]
    elif suffix == ".pdf":
        loaded_pages = PyPDFLoader(str(path)).load()
        pages = [(loaded_page.metadata.get("page", index) + 1, loaded_page.page_content)
                 for index, loaded_page in enumerate(loaded_pages)]
    elif suffix == ".docx":
        word_document = docx.Document(str(path))
        paragraph_texts = [
            paragraph.text for paragraph in word_document.paragraphs]
        table_texts = []
        for table in word_document.tables:
            for row in table.rows:
                table_texts.append(" | ".join(cell.text for cell in row.cells))
        pages = [(None, "\n".join(paragraph_texts + table_texts))]
    else:
        return []
    return [(page_number, page_text) for page_number, page_text in pages if page_text.strip()]


def fixed_chunk_text(pages, chunk_size=1000, overlap=200):
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=overlap)
    chunks = []
    for page_number, page_text in pages:
        for chunk_text in text_splitter.split_text(page_text):
            chunks.append((page_number, chunk_text))
    return chunks


def index_chunks(connection, document_id, notice_id, chunks):
    chunk_rows = [
        {"chunk_text": chunk_text, "document_id": document_id,
         "notice_id": notice_id, "page_number": page_number}
        for page_number, chunk_text in chunks]
    connection.execute(
        """DELETE FROM document_chunks WHERE document_id = :document_id""",
        {"document_id": document_id})
    connection.executemany(
        """INSERT INTO document_chunks (chunk_text, document_id, notice_id, page_number)
        VALUES (:chunk_text, :document_id, :notice_id, :page_number)""", chunk_rows)
    connection.commit()


def index_pending(connection, folder):
    successfully_indexed_documents = 0
    documents = connection.execute(
        """SELECT id, notice_id, filename FROM documents WHERE status = :status""",
        {"status": "downloaded"}).fetchall()
    for document_id, notice_id, filename in documents:
        try:
            if not filename:
                set_document_status(connection, document_id, "failed")
            file_extension = Path(filename).suffix.lower()
            path = Path(folder) / f"{document_id}{file_extension}"
            pages = extract_pages(path)
            if not pages:
                set_document_status(connection, document_id, "no_text")
            else:
                chunked_pages = fixed_chunk_text(pages)
                index_chunks(connection, document_id, notice_id, chunked_pages)
                set_document_status(connection, document_id, "indexed")
                successfully_indexed_documents += 1
        except Exception as error:
            connection.rollback()
            set_document_status(connection, document_id,
                                "failed", str(error)[:500])
    return successfully_indexed_documents


async def main():
    samclient = SamClient()
    try:
        connection = init_db(settings.db_path)
        try:
            registered_docs = register_documents(
                connection, date.today().isoformat(), settings.target_naics_codes)
            print(f"Registered {registered_docs} documents")
            processed_count = await download_pending(samclient, connection, settings.documents_dir, max_downloads=settings.max_attachment_downloads_per_run, max_bytes=settings.max_attachment_bytes, counts_toward_quota=settings.attachment_requests_count_toward_quota)
            print(f"Processed {processed_count} documents")
            indexed_documents = index_pending(
                connection, settings.documents_dir)
            print(f"Indexed {indexed_documents} documents")
        finally:
            connection.close()
    finally:
        await samclient.aclose()


if __name__ == "__main__":
    asyncio.run(main())
