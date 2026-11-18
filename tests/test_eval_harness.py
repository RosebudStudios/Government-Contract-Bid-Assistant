"""Tests for the eval harness itself. They need no API key and make no model calls:
the fixture is checked against the real tools, and the runner is driven by a scripted model."""
import asyncio
import importlib
from datetime import date

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from evals import run_evals
from evals.cases import EVAL_CASES, EvalCase
from evals.fixture import INJECTION_TOKEN, build_fixture
from evals.run_evals import (extract_answer_text, extract_tool_names, patched_tool_settings,
                             run_case, score_case, summarize)
from src.agent import build_agent
from src.settings import Settings
from src.tools import LOCAL_TOOLS

TODAY = date(2026, 11, 14)


@pytest.fixture
def fixture_paths(tmp_path):
    return build_fixture(tmp_path, today=TODAY)


def call_real_tool(tool_name, tool_arguments, fixture_paths):
    database_path, profile_path = fixture_paths
    selected_tool = next(
        tool for tool in LOCAL_TOOLS if tool.name == tool_name)
    with patched_tool_settings(database_path, profile_path):
        return selected_tool.invoke(tool_arguments)


# ------------------------------------------------------------------ the cases themselves

def test_case_ids_are_unique_and_every_case_checks_something():
    case_ids = [case.case_id for case in EVAL_CASES]
    assert len(case_ids) == len(set(case_ids))
    for case in EVAL_CASES:
        assert (case.must_call or case.must_not_call or case.must_mention
                or case.must_not_mention or case.max_tool_calls is not None), case.case_id


# ------------------------------------------------------------------ score_case

def test_a_case_that_meets_every_check_passes():
    case = EvalCase("c", "p", must_call=("score_opportunity",), must_not_call=("get_opportunity",),
                    max_tool_calls=2, must_mention=(("strong fit", "possible fit"),),
                    must_not_mention=("not eligible",))
    assert score_case(case, ["score_opportunity"],
                      "This is a Strong Fit.") == []


def test_each_kind_of_violation_is_reported():
    case = EvalCase("c", "p", must_call=("score_opportunity",), must_not_call=("get_opportunity",),
                    max_tool_calls=1, must_mention=(("strong fit",),),
                    must_not_mention=("not eligible",))
    failures = score_case(
        case, ["get_opportunity", "get_opportunity"], "Not eligible.")
    joined = " | ".join(failures)
    assert "did not call score_opportunity" in joined
    assert "called get_opportunity" in joined
    assert "2 tool calls (limit 1)" in joined
    assert "mentions none of" in joined
    assert "contains 'not eligible'" in joined


def test_every_group_of_must_mention_has_to_be_satisfied():
    case = EvalCase("c", "p", must_mention=(("alpha", "beta"), ("gamma",)))
    assert score_case(case, [], "alpha and gamma") == []
    assert len(score_case(case, [], "alpha only")) == 1


def test_phrase_matching_ignores_case():
    case = EvalCase("c", "p", must_mention=(("Statement_of_Work.pdf",),),
                    must_not_mention=("NOT ELIGIBLE",))
    assert score_case(case, [], "see statement_of_work.PDF") == []
    assert score_case(case, [], "statement_of_work.pdf is not eligible") != []


def test_zero_tool_calls_allowed_means_any_call_fails():
    case = EvalCase("c", "p", max_tool_calls=0)
    assert score_case(case, [], "") == []
    assert score_case(case, ["search_opportunities"], "") != []


# ------------------------------------------------------------------ message extraction

def test_tool_names_and_final_answer_come_from_ai_messages_only():
    messages = [
        HumanMessage(content="hi"),
        AIMessage(content="", tool_calls=[{"name": "search_opportunities", "args": {}, "id": "1"},
                                          {"name": "score_opportunity", "args": {}, "id": "2"}]),
        ToolMessage(content="result", tool_call_id="1"),
        AIMessage(content=[{"type": "text", "text": "Final text"}]),
    ]
    assert extract_tool_names(messages) == [
        "search_opportunities", "score_opportunity"]
    assert extract_answer_text(messages) == "Final text"
    assert extract_answer_text([HumanMessage(content="only a question")]) == ""


# ------------------------------------------------------------------ the fixture really exercises each path

def statuses(tool_output):
    """{'naics_code': 'pass', ...} parsed from score_opportunity's text."""
    found = {}
    for line in tool_output.splitlines():
        name, separator, rest = line.partition(": ")
        if separator and rest.split(" - ")[0] in ("pass", "fail", "unknown"):
            found[name] = rest.split(" - ")[0]
    return found


def test_the_fit_notice_passes_every_hard_check_and_has_cited_evidence(fixture_paths):
    output = call_real_tool("score_opportunity", {
                            "notice_id": "EVAL-FIT-001"}, fixture_paths)
    assert set(statuses(output).values()) == {"pass"}
    assert "Statement_of_Work.pdf" in output and "page 3" in output


def test_the_naics_notice_fails_only_on_naics_and_stops(fixture_paths):
    output = call_real_tool("score_opportunity", {
                            "notice_id": "EVAL-NAICS-002"}, fixture_paths)
    results = statuses(output)
    assert results["naics_code"] == "fail"
    assert [name for name, status in results.items() if status == "fail"] == [
        "naics_code"]
    assert "Stopped after the hard checks" in output


def test_the_deadline_notice_fails_only_on_deadline(fixture_paths):
    output = call_real_tool("score_opportunity", {
                            "notice_id": "EVAL-DEADLINE-003"}, fixture_paths)
    results = statuses(output)
    assert [name for name, status in results.items() if status == "fail"] == [
        "deadline"]


def test_the_excluded_notice_passes_the_hard_checks_but_hits_excluded_keywords(fixture_paths):
    output = call_real_tool("score_opportunity", {
                            "notice_id": "EVAL-EXCLUDED-004"}, fixture_paths)
    assert "fail" not in statuses(output).values()
    assert "Excluded Hits in Title: construction (1x), janitorial (1x)" in output


def test_the_unknown_notice_has_an_unknown_state_and_no_failures(fixture_paths):
    output = call_real_tool("score_opportunity", {
                            "notice_id": "EVAL-UNKNOWN-005"}, fixture_paths)
    results = statuses(output)
    assert results["states_served"] == "unknown"
    assert "fail" not in results.values()


def test_the_no_documents_notice_reports_nothing_indexed(fixture_paths):
    output = call_real_tool("score_opportunity", {
                            "notice_id": "EVAL-NODOCS-006"}, fixture_paths)
    assert "fail" not in statuses(output).values()
    assert "No documents are indexed for this notice." in output


def test_the_injection_text_reaches_the_agent_but_only_inside_the_fence(fixture_paths):
    output = call_real_tool("score_opportunity", {
                            "notice_id": "EVAL-INJECT-007"}, fixture_paths)
    fenced = output.split("<document_excerpts>")[
        1].split("</document_excerpts>")[0]
    assert INJECTION_TOKEN in fenced
    assert INJECTION_TOKEN not in output.replace(fenced, "")


def test_the_clearance_notice_passes_the_hard_checks_but_its_attachment_holds_the_disqualifier(fixture_paths):
    scored = call_real_tool("score_opportunity", {
                            "notice_id": "EVAL-CLEARANCE-008"}, fixture_paths)
    assert "fail" not in statuses(scored).values()
    # the capability evidence alone does not reveal it
    assert "Top Secret" not in scored
    searched = call_real_tool("search_solicitation_documents",
                              {"query": "facility clearance", "notice_id": "EVAL-CLEARANCE-008"}, fixture_paths)
    assert "active Top Secret" in searched and "$500 million" in searched


def test_a_missing_notice_is_reported_as_missing(fixture_paths):
    output = call_real_tool("score_opportunity", {
                            "notice_id": "EVAL-DOES-NOT-EXIST"}, fixture_paths)
    assert "No notice found" in output


def test_document_search_finds_the_fit_notice(fixture_paths):
    output = call_real_tool("search_solicitation_documents",
                            {"query": "cloud migration"}, fixture_paths)
    assert "EVAL-FIT-001" in output


def test_search_by_naics_finds_the_seeded_notices(fixture_paths):
    output = call_real_tool("search_opportunities", {
                            "naics_code": "541512"}, fixture_paths)
    assert "EVAL-FIT-001" in output and "EVAL-NAICS-002" not in output


@pytest.fixture
def todays_fixture_paths(tmp_path):
    """The tools use the real current date, so these tests build the fixture relative to it."""
    return build_fixture(tmp_path, today=date.today())


def test_the_shortlist_keeps_the_clean_notices_and_drops_the_failures(todays_fixture_paths):
    output = call_real_tool("shortlist_opportunities",
                            {"limit": 10}, todays_fixture_paths)
    # EVAL-NAICS-002 (335311) is never scanned: the company's NAICS codes are applied up front
    assert ("Scanned 7 open notices. Passed every hard check: 5. "
            "No failures but some unknown fields: 1. Failed at least one check: 1.") in output
    assert "Failures by check (a notice can fail several): deadline 1." in output
    for kept in ("EVAL-FIT-001", "EVAL-EXCLUDED-004", "EVAL-NODOCS-006", "EVAL-INJECT-007",
                 "EVAL-UNKNOWN-005", "EVAL-UNKNOWN-005"):
        assert kept in output
    for dropped in ("EVAL-NAICS-002", "EVAL-DEADLINE-003"):
        assert dropped not in output


def test_the_shortlist_puts_the_notice_with_an_unknown_field_last(todays_fixture_paths):
    output = call_real_tool("shortlist_opportunities",
                            {"limit": 10}, todays_fixture_paths)
    assert output.index("EVAL-UNKNOWN-005") > output.index("EVAL-INJECT-007")
    assert "unknown: states_served (no place of performance listed)" in output


def test_the_shortlist_can_be_narrowed_to_one_naics_code(todays_fixture_paths):
    output = call_real_tool("shortlist_opportunities", {
                            "naics_code": "541511"}, todays_fixture_paths)
    assert "Scanned 1 open notices" in output and "EVAL-UNKNOWN-005" in output


def test_patched_settings_are_restored_afterwards(fixture_paths):
    module = importlib.import_module("src.tools.score_opportunity")
    before = module.settings
    with patched_tool_settings(*fixture_paths):
        assert module.settings is not before
    assert module.settings is before


def test_the_refresh_tool_is_never_given_to_the_eval_agent():
    assert "refresh_opportunities" not in [
        tool.name for tool in run_evals.eval_tools()]


# ------------------------------------------------------------------ the runner, driven by a scripted model

class ScriptedModel(FakeMessagesListChatModel):
    """Replays fixed replies and accepts bind_tools, so the real agent graph can run offline."""

    def bind_tools(self, tools, **kwargs):
        return self


def scripted_agent(replies):
    return build_agent(run_evals.eval_tools(), Settings(), checkpointer=InMemorySaver(),
                       model=ScriptedModel(responses=replies), system_prompt="test prompt")


def tool_call_reply(tool_name, tool_arguments):
    return AIMessage(content="", tool_calls=[
        {"name": tool_name, "args": tool_arguments, "id": "call_1"}])


def test_a_scripted_good_run_passes_and_records_the_tool_calls(fixture_paths):
    case = next(case for case in EVAL_CASES if case.case_id ==
                "fit-with-evidence")
    agent = scripted_agent([
        tool_call_reply("score_opportunity", {"notice_id": "EVAL-FIT-001"}),
        AIMessage(content="Strong fit. Evidence: Statement_of_Work.pdf, page 3.")])
    with patched_tool_settings(*fixture_paths):
        result = asyncio.run(run_case(agent, case, Settings()))
    assert result.passed, result.failures
    assert result.tool_names == ["score_opportunity"]


def test_a_scripted_run_that_obeys_an_injection_fails_the_case(fixture_paths):
    case = next(case for case in EVAL_CASES if case.case_id ==
                "document-injection-ignored")
    agent = scripted_agent([
        tool_call_reply("score_opportunity", {"notice_id": "EVAL-INJECT-007"}),
        AIMessage(content=INJECTION_TOKEN)])
    with patched_tool_settings(*fixture_paths):
        result = asyncio.run(run_case(agent, case, Settings()))
    assert not result.passed
    assert any("mentions none of" in failure for failure in result.failures)


def test_the_input_guard_case_passes_with_no_model_at_all(fixture_paths):
    case = next(case for case in EVAL_CASES if case.case_id ==
                "input-guard-blocks-injection")
    agent = scripted_agent(
        [AIMessage(content="the model should never be reached")])
    with patched_tool_settings(*fixture_paths):
        result = asyncio.run(run_case(agent, case, Settings()))
    assert result.passed, result.failures
    assert result.tool_names == []


def test_a_crashing_agent_is_a_failed_case_not_a_crashed_eval():
    class ExplodingAgent:
        async def ainvoke(self, payload, config):
            raise RuntimeError("boom")

    case = EvalCase("c", "p", must_mention=(("x",),))
    result = asyncio.run(run_case(ExplodingAgent(), case, Settings()))
    assert not result.passed
    assert "RuntimeError" in result.failures[0]


# ------------------------------------------------------------------ reporting

def test_the_summary_counts_passes_per_case_and_overall():
    cases = [EvalCase("a", "p"), EvalCase("b", "p")]
    results = [run_evals.CaseResult("a", True), run_evals.CaseResult("a", False, ["did not call x"]),
               run_evals.CaseResult("b", True), run_evals.CaseResult("b", True)]
    summary = summarize(results, cases, model_name="test-model", repeat=2)
    assert summary["total_passes"] == 3 and summary["total_runs"] == 4
    assert summary["pass_rate"] == 0.75
    by_id = {entry["case_id"]: entry for entry in summary["per_case"]}
    assert by_id["a"]["passes"] == 1 and by_id["a"]["failures"] == [
        "did not call x"]
    assert by_id["b"]["passes"] == 2
    report = run_evals.format_report(summary)
    assert "3/4 runs passed (75%)" in report and "| a | 1/2 | did not call x |" in report


def test_the_command_line_rejects_a_zero_repeat():
    with pytest.raises(SystemExit):
        run_evals.parse_arguments(["--repeat", "0"])


# ------------------------------------------------------------------ the demo

def test_the_demo_answers_one_question_on_synthetic_data(monkeypatch, capsys):
    from evals import demo

    real_build_agent = demo.build_agent

    def scripted_build_agent(tools, settings, **keyword_arguments):
        replies = [tool_call_reply("shortlist_opportunities", {"naics_code": "541512"}),
                   AIMessage(content="Top candidate: EVAL-FIT-001.")]
        keyword_arguments["model"] = ScriptedModel(responses=replies)
        return real_build_agent(tools, settings, **keyword_arguments)

    monkeypatch.setattr(demo, "build_agent", scripted_build_agent)
    demo.main(["--question", "Find open notices in NAICS 541512."])
    printed = capsys.readouterr().out
    assert "You: Find open notices in NAICS 541512." in printed
    assert "Top candidate: EVAL-FIT-001." in printed


def test_the_demo_banner_says_the_data_is_invented():
    from evals import demo
    assert "invented" in demo.BANNER and "quit" in demo.BANNER
    assert all(question in demo.BANNER for question in demo.SUGGESTED_QUESTIONS)
