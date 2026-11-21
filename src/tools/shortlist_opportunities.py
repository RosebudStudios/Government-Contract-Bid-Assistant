import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.settings import Settings
from src.tools.get_opportunity import clean_description
from src.tools.score_opportunity import (NOTICE_COLUMNS, find_keyword_hits, format_hits,
                                         load_company_profile, run_hard_checks)

settings = Settings()

MAX_RESULTS = 10
MAX_NOTICES_SCANNED = 2000


class ShortlistOpportunitiesInput(BaseModel):
    naics_code: str | None = Field(
        default=None,
        description="Only consider notices with this exact six-digit NAICS code, for example '541512'. "
                    "Leave empty to consider every open notice.")
    keyword: str | None = Field(
        default=None,
        description="Only consider notices whose title or description contains this word or phrase.")
    limit: int = Field(
        default=5, ge=1, le=MAX_RESULTS,
        description=f"How many notices to return, 1 to {MAX_RESULTS}. Use 1 or 2 when the user asks for 'the best' one.")


@dataclass(frozen=True)
class ShortlistResult:
    scanned_count: int
    failed_notice_count: int
    passed_all_count: int
    unknown_only_count: int
    failed_counts: dict      # check name -> how many notices failed that check
    # [(notice dict, [CheckResult, ...]), ...], best first
    shortlisted: list        # [ShortlistEntry, ...], best first
    naics_scope: str = ""    # plain-language note on which NAICS codes were considered


class ShortlistEntry(NamedTuple):
    notice: dict
    results: list           # hard-check CheckResult objects
    capability_hits: dict   # capability phrase -> times it appears in the title/description


def notice_type_rank(notice, company_profile):
    """Lower is better: the notice type's position in the profile's notice_types list.

    The order of that list is a preference order, so list the types you most want first."""
    preferred_types = company_profile.notice_types
    if notice["type"] in preferred_types:
        return preferred_types.index(notice["type"])
    return len(preferred_types)


def find_notice_capability_hits(notice, company_profile):
    description_text = notice["description_text"]
    notice_description = clean_description(
        description_text) if description_text else ""
    searchable_text = f"{notice['title'] or ''}: {notice_description}"
    return find_keyword_hits(searchable_text, company_profile.capabilities)


def fetch_open_notices(connection, *, today, naics_code=None, company_naics_codes=(), keyword=None):
    """Notices whose response deadline is today or later, newest posted first.

    An explicit naics_code wins. Otherwise, if the company lists NAICS codes, only those codes
    are scanned, plus notices with no NAICS code at all (those are 'unknown', not failed).
    Without this the scan would fill up with notices that can never match.
    """
    conditions = ["substr(response_deadline, 1, 10) >= :today_text"]
    parameters = {"today_text": today.isoformat(),
                  "scan_limit": MAX_NOTICES_SCANNED}
    if naics_code:
        conditions.append("naics_code = :naics_code")
        parameters["naics_code"] = naics_code
    elif company_naics_codes:
        naics_parameters = {f"company_naics_{index}": code
                            for index, code in enumerate(company_naics_codes)}
        naics_placeholders = ", ".join(f":{name}" for name in naics_parameters)
        conditions.append(
            f"(naics_code IN ({naics_placeholders}) OR naics_code IS NULL)")
        parameters.update(naics_parameters)
    if keyword:
        conditions.append(
            "(title LIKE :keyword_pattern OR description_text LIKE :keyword_pattern)")
        parameters["keyword_pattern"] = f"%{keyword}%"
    column_list = ", ".join(NOTICE_COLUMNS)
    where_clause = " AND ".join(conditions)
    rows = connection.execute(
        f"""SELECT {column_list} FROM solicitations
        WHERE {where_clause}
        ORDER BY posted_date DESC
        LIMIT :scan_limit""", parameters).fetchall()
    return [dict(zip(NOTICE_COLUMNS, row)) for row in rows]


def shortlist_notices(connection, company_profile, *, today, naics_code=None, keyword=None, limit=5):
    notices = fetch_open_notices(
        connection, today=today, naics_code=naics_code,
        company_naics_codes=company_profile.naics_codes, keyword=keyword)
    failed_counts = Counter()
    failed_notice_count = 0
    clean_entries = []   # [(unknown_count, scan_position, ShortlistEntry)]
    for scan_position, notice in enumerate(notices):
        results = run_hard_checks(notice, company_profile, today)
        failed_check_names = [
            result.name for result in results if result.status == "fail"]
        if failed_check_names:
            failed_notice_count += 1
            failed_counts.update(failed_check_names)
            continue
        unknown_count = sum(result.status == "unknown" for result in results)
        entry = ShortlistEntry(notice, results,
                               find_notice_capability_hits(notice, company_profile))
        clean_entries.append((unknown_count, scan_position, entry))

    def ranking_key(clean_entry):
        unknown_count, scan_position, entry = clean_entry
        return (unknown_count,                                    # fewest unknown fields first
                # then the preferred notice types
                notice_type_rank(entry.notice, company_profile),
                # then more capability matches
                -len(entry.capability_hits),
                # then newest posted (scan order)
                scan_position)

    clean_entries.sort(key=ranking_key)
    passed_all_count = sum(1 for unknown_count, _,
                           _ in clean_entries if unknown_count == 0)
    result_count = max(1, min(limit, MAX_RESULTS))
    shortlisted = [entry for _, _, entry in clean_entries[:result_count]]
    if naics_code:
        naics_scope = f"Only NAICS code {naics_code} was considered."
    elif company_profile.naics_codes:
        naics_scope = (f"Only the company's NAICS codes ({', '.join(company_profile.naics_codes)}) "
                       "and notices with no NAICS code were considered.")
    else:
        naics_scope = ""
    return ShortlistResult(
        scanned_count=len(notices), failed_notice_count=failed_notice_count,
        passed_all_count=passed_all_count,
        unknown_only_count=len(clean_entries) - passed_all_count,
        failed_counts=dict(failed_counts), shortlisted=shortlisted, naics_scope=naics_scope)


def format_notice_line(entry):
    notice, results = entry.notice, entry.results
    deadline = (notice["response_deadline"] or "not listed")[:10]
    unknown_details = [f"{result.name} ({result.detail})"
                       for result in results if result.status == "unknown"]
    unknown_text = "; ".join(unknown_details) if unknown_details else "none"
    return (f"notice_id: {notice['notice_id']}\n"
            f"title: {notice['title']}\n"
            f"type: {notice['type'] or 'not listed'} | naics_code: {notice['naics_code'] or 'not listed'} | "
            f"set_aside: {notice['type_of_set_aside'] or 'none'} | "
            f"state: {notice['pop_state'] or 'not listed'} | response_deadline: {deadline}\n"
            f"capability_hits: {format_hits(entry.capability_hits)}\n"
            f"unknown: {unknown_text}")


def format_shortlist(result):
    lines = [f"Scanned {result.scanned_count} open notices. "
             f"Passed every hard check: {result.passed_all_count}. "
             f"No failures but some unknown fields: {result.unknown_only_count}. "
             f"Failed at least one check: {result.failed_notice_count}."]
    if result.naics_scope:
        lines.append(result.naics_scope)
    if result.failed_counts:
        failure_text = ", ".join(f"{name} {count}"
                                 for name, count in sorted(result.failed_counts.items()))
        lines.append(
            f"Failures by check (a notice can fail several): {failure_text}.")
    if result.scanned_count >= MAX_NOTICES_SCANNED:
        lines.append("The scan limit was reached, so some open notices were not "
                     "checked. Narrow the search with naics_code or keyword.")
    if not result.shortlisted:
        lines.append("No open notice passed the hard checks.")
        return "\n".join(lines)
    lines.append(f"Top {len(result.shortlisted)} (fewest unknown fields, then preferred notice type, "
                 "then most capability matches, then newest posted):")
    lines.append("")
    lines.append("\n\n".join(format_notice_line(entry)
                 for entry in result.shortlisted))
    lines.append("")
    lines.append("Next step: run score_opportunity on a notice_id above for "
                 "capability evidence and attachment citations.")
    return "\n".join(lines)


@tool("shortlist_opportunities", args_schema=ShortlistOpportunitiesInput)
def shortlist_opportunities(naics_code=None, keyword=None, limit=5) -> str:
    """Find open notices that clear the company's hard requirements (NAICS code, set-aside, state,
    notice type, response deadline). Checks every open notice in plain code, drops the ones that
    fail, and returns the best few with any unknown fields flagged. Use this FIRST when asked to
    find or recommend opportunities, then run score_opportunity on one or two of the results."""
    company_profile = load_company_profile(settings.profile_path)
    read_only_uri = settings.db_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(read_only_uri, uri=True)) as connection:
        result = shortlist_notices(connection, company_profile, today=date.today(),
                                   naics_code=naics_code, keyword=keyword, limit=limit)
    return format_shortlist(result)
