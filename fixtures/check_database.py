import sqlite3
from datetime import date

from src.settings import Settings

settings = Settings()
today_text = date.today().isoformat()
connection = sqlite3.connect(settings.db_path)

print("usage today:", connection.execute(
    "SELECT request_count FROM api_usage WHERE day = :day", {"day": today_text}).fetchone())
print("descriptions saved:", connection.execute(
    "SELECT COUNT(*) FROM solicitations WHERE description_text IS NOT NULL").fetchone()[0])
print("empty-string descriptions:", connection.execute(
    "SELECT COUNT(*) FROM solicitations WHERE description_text = ''").fetchone()[0])
print("still waiting for a description:", connection.execute(
    """SELECT COUNT(*) FROM solicitations
       WHERE description_text IS NULL AND description LIKE 'http%'
         AND substr(response_deadline, 1, 10) >= :day""", {"day": today_text}).fetchone()[0])
connection.close()
