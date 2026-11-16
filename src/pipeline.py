import asyncio
from datetime import date

from src.ingestion import (RateLimitError, SamClient, fetch_descriptions,
                           init_db, parse_args, run_sync)
from src.settings import Settings

settings = Settings()


async def ingestion_pipeline(days: int = 1, max_pages: int | None = None) -> str:
    samclient = SamClient()
    today = date.today()
    summary_lines = []
    try:
        connection = init_db(settings.db_path)
        try:
            try:
                upserted_count = await run_sync(
                    samclient, connection, days=days, max_pages=max_pages, today=today,
                    lookback_days=settings.sync_lookback_days,
                    daily_request_limit=settings.daily_request_limit)
                summary_lines.append(
                    f"Notices added or updated: {upserted_count}.")
                saved_count = await fetch_descriptions(
                    samclient, connection, today,
                    daily_request_limit=settings.daily_request_limit,
                    max_fetches=settings.description_requests_per_run,
                    target_naics_codes=settings.target_naics_codes)
                summary_lines.append(f"Descriptions saved: {saved_count}.")
            except RateLimitError:
                summary_lines.append(
                    "SAM.gov's daily request limit was reached, so the refresh stopped early. Try again tomorrow.")
        finally:
            connection.close()
    finally:
        await samclient.aclose()
    return " ".join(summary_lines)


if __name__ == "__main__":
    command_line_args = parse_args()
    print(asyncio.run(ingestion_pipeline(
        command_line_args.days, command_line_args.max_pages)))
