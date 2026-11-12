import sqlite3
from src.settings import Settings

settings = Settings()
connection = sqlite3.connect(settings.db_path)
repairs = {
    "no_text documents back to downloaded": """UPDATE documents SET status = 'downloaded'
        WHERE status = 'no_text'""",
    "Word files back to pending": """UPDATE documents SET status = 'pending'
        WHERE status = 'unsupported' AND lower(filename) LIKE '%.docx'""",
    "document 9 back to pending": """UPDATE documents SET status = 'pending', error = NULL
        WHERE id = 9""",
}
for description, sql in repairs.items():
    cursor = connection.execute(sql)
    print(description, cursor.rowcount)
connection.commit()
connection.close()
