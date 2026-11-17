import tomllib
import sqlite3
import re
from contextlib import closing
from dataclasses import dataclass
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from datetime import date
from src.company_profile import CompanyProfile

from src.settings import Settings
from src.tools.search_solicitation_documents import search_chunks, fence_excerpts
from src.tools.get_opportunity import clean_description


settings = Settings()

NOTICE_COLUMNS = ("notice_id", "title", "naics_code", "type_of_set_aside",
                  "pop_state", "type", "response_deadline", "description_text")


class ScoreOpportunityInput(BaseModel):
    notice_id: str = Field(
        description="The notice id of the solicitation to score, as shown in search results.")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str      # "pass", "fail" or "unknown"
    detail: str


def load_company_profile(path):
    with open(path, "rb") as profile_file:
        raw_profile = tomllib.load(profile_file)
    company_profile = CompanyProfile.model_validate(raw_profile)
    return company_profile


def fetch_notice(connection, notice_id):
    column_list = ", ".join(NOTICE_COLUMNS)
    row = connection.execute(
        f"SELECT {column_list} FROM solicitations WHERE notice_id = :notice_id",
        {"notice_id": notice_id}).fetchone()
    if row is None:
        return None
    return dict(zip(NOTICE_COLUMNS, row))


def days_until_deadline(response_deadline, today):
    if not response_deadline:
        return None
    return (date.fromisoformat(response_deadline[:10]) - today).days


def check_naics_codes(notice, company_profile):
    notice_naics_code = notice["naics_code"]
    if notice_naics_code is None:
        return CheckResult("naics_code", "unknown", "no naics code listed")
    if notice_naics_code in company_profile.naics_codes:
        return CheckResult("naics_code", "pass", f"company serves {notice_naics_code}")
    if not company_profile.naics_codes:
        return CheckResult("naics_code", "pass", "no company naics codes listed, empty means no restriction")
    return CheckResult("naics_code", "fail",
                       f"naics code for {notice_naics_code}, which the company does not serve")


def check_set_aside(notice, company_profile):
    notice_set_aside = notice["type_of_set_aside"]
    if notice_set_aside is None:
        return CheckResult("set_aside", "pass", "no set-aside, open to all")
    if notice_set_aside in company_profile.set_aside_eligibilities:
        return CheckResult("set_aside", "pass", f"company is eligible for {notice_set_aside}")
    return CheckResult("set_aside", "fail",
                       f"set aside for {notice_set_aside}, which the company is not eligible for")


def check_states_served(notice, company_profile):
    notice_state = notice["pop_state"]
    if notice_state is None:
        return CheckResult("states_served", "unknown", "no place of performance listed")
    served_states = [state.lower() for state in company_profile.states_served]
    if not served_states:
        return CheckResult("states_served", "pass", "no company states listed, empty means no restriction")
    if notice_state.lower() in served_states:
        return CheckResult("states_served", "pass", f"company serves {notice_state}")
    return CheckResult("states_served", "fail", f"place of performance {notice_state} is not one the company serves")


def check_notice_types(notice, company_profile):
    notice_type = notice["type"]
    if notice_type is None:
        return CheckResult("type", "unknown", "no type listed")
    if notice_type in company_profile.notice_types:
        return CheckResult("type", "pass", f"company is eligible for {notice_type}")
    if not company_profile.notice_types:
        return CheckResult("type", "pass", "no company notice types listed, empty means no restriction")
    return CheckResult("type", "fail",
                       f"type for {notice_type}, which the company is not eligible for")


def check_deadline(notice, company_profile, today):
    days_left = days_until_deadline(notice["response_deadline"], today)
    if days_left is None:
        return CheckResult("deadline", "unknown", "no response deadline listed")
    if days_left >= company_profile.minimum_days_to_respond:
        return CheckResult("deadline", "pass", f"{days_left} days left")
    return CheckResult("deadline", "fail",
                       f"{days_left} days left, minimum is {company_profile.minimum_days_to_respond}")


def find_keyword_hits(text, keywords):
    hits = {}
    for keyword in keywords:
        pattern = re.compile(
            r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", re.IGNORECASE)
        hit_count = len(pattern.findall(text))
        if hit_count:
            hits[keyword] = hit_count
    return hits


def find_capability_evidence(connection, notice_id, capabilities):
    evidence_rows = {}
    for capability in capabilities:
        phrase_query = '"' + capability.replace('"', '""') + '"'
        rows = search_chunks(connection, query=phrase_query,
                             notice_id=notice_id, result_limit=1)
        if rows:
            evidence_rows[capability] = rows[0]
    return evidence_rows


def format_evidence(rows, file_count) -> str:
    if not rows:
        if file_count == 0:
            return "No documents are indexed for this notice."
        else:
            return "No matching text found in the indexed documents."
    blocks = []
    for capability, row in rows.items():
        notice_id, title, filename, page_number, excerpt = row
        if page_number is None:
            location = "Word document (no page numbers)"
        else:
            location = f"page {page_number}"
        clean_excerpt = " ".join(excerpt.split())
        blocks.append(
            f"{capability}: {filename}, {location} - {clean_excerpt}")
    return fence_excerpts("\n\n".join(blocks))


def run_hard_checks(notice, company_profile, today):
    return [check_naics_codes(notice, company_profile),
            check_set_aside(notice, company_profile),
            check_states_served(notice, company_profile),
            check_notice_types(notice, company_profile),
            check_deadline(notice, company_profile, today)]


def run_soft_checks(connection, company_profile, notice):
    missing_information = []
    notice_title = notice["title"] or ""
    description_text = notice["description_text"]
    if description_text is None:
        notice_description = ""
        missing_information.append("description not downloaded yet")
    else:
        notice_description = clean_description(description_text)
    file_count = connection.execute(
        """SELECT count(*) FROM documents
                WHERE notice_id = :notice_id 
                AND status = 'indexed'
                """, {"notice_id": notice["notice_id"]}
    ).fetchone()[0]

    capability_hits = find_keyword_hits(
        f"{notice_title}: {notice_description}", company_profile.capabilities)
    excluded_keywords_hits_title = find_keyword_hits(
        notice_title, company_profile.excluded_keywords)
    excluded_keywords_hits_description = find_keyword_hits(
        notice_description, company_profile.excluded_keywords)

    evidence = format_evidence(find_capability_evidence(
        connection, notice["notice_id"], company_profile.capabilities), file_count)

    results = {"capability_hits": capability_hits, "excluded_hits_in_title": excluded_keywords_hits_title,
               "excluded_hits_in_description": excluded_keywords_hits_description, "evidence": evidence, "missing_information": missing_information}
    return results


def format_hits(hits):
    if not hits:
        return "none"
    return ", ".join(f"{keyword} ({hit_count}x)" for keyword, hit_count in hits.items())


def format_missing_information(missing_info):
    if not missing_info:
        return "none"
    return "; ".join(missing_info)


def format_results(hard_results, *, soft_results=None, summary=""):
    lines = [
        f"{result.name}: {result.status} - {result.detail}" for result in hard_results]
    if soft_results is not None:
        capability_hits = format_hits(soft_results["capability_hits"])
        excluded_hits_in_title = format_hits(
            soft_results["excluded_hits_in_title"])
        excluded_hits_in_description = format_hits(
            soft_results["excluded_hits_in_description"])
        evidence = soft_results["evidence"]
        missing_information = format_missing_information(
            soft_results["missing_information"])
        lines.append(
            f"Capability Hits: {capability_hits}\nExcluded Hits in Title: {excluded_hits_in_title}\nExcluded Hits in Description: {excluded_hits_in_description}\nEvidence: {evidence}\nMissing Information: {missing_information}")
    else:
        lines.append(
            "Stopped after the hard checks: this notice does not fit so no further analysis was done.")
    if summary != "":
        lines.append(f"Company Summary: {summary}")
    return "\n".join(lines)


def hard_checks_failed(hard_results):
    return any(result.status == "fail" for result in hard_results)


@tool("score_opportunity", args_schema=ScoreOpportunityInput)
def score_opportunity(notice_id):
    """Compare a solicitation's information to a company profile file's data. Returns hard-check results and evidence. Stops early if the hard check fails. The model should make the final judgement based on the results."""
    company_profile = load_company_profile(settings.profile_path)
    read_only_uri = settings.db_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(read_only_uri, uri=True)) as connection:
        notice = fetch_notice(connection, notice_id)
        if notice is None:
            return f"No notice found with id {notice_id}"
        hard_results = run_hard_checks(notice, company_profile, date.today())
        if hard_checks_failed(hard_results):
            return format_results(hard_results)
        soft_results = run_soft_checks(
            connection, company_profile, notice)
    return format_results(hard_results, soft_results=soft_results, summary=company_profile.summary)
