"""Runs the scripted evaluation against the real agent and reports a pass rate.

    python -m evals.run_evals                 # one run of every case
    python -m evals.run_evals --repeat 3      # three runs per case (the model is not deterministic)
    python -m evals.run_evals --only fit-with-evidence

The agent is built exactly as in production (same middleware, same system prompt, same tools)
except that: it reads a small synthetic database (see evals/fixture.py), and the
refresh_opportunities tool is left out so an eval can never spend SAM.gov quota.
Needs ANTHROPIC_API_KEY. Each run makes live model calls and costs a little.
"""
import argparse
import asyncio
import importlib
import json
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from evals.cases import EVAL_CASES
from evals.fixture import build_fixture
from src.agent import build_agent, result_text
from src.settings import Settings
from src.tools import LOCAL_TOOLS

# The modules that each read their own module-level `settings`. They are loaded with
# import_module because src.tools re-exports tool OBJECTS under the same names, which would
# shadow the modules for a plain `import`.
TOOL_MODULES_USING_SETTINGS = (
    "src.tools.search_opportunities",
    "src.tools.get_opportunity",
    "src.tools.search_solicitation_documents",
    "src.tools.score_opportunity",
)
EXCLUDED_TOOL_NAMES = ("refresh_opportunities",)


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    failures: list = field(default_factory=list)
    tool_names: list = field(default_factory=list)
    answer_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


# ------------------------------------------------------------------ pure scoring helpers

def extract_tool_names(messages):
    """Names of every tool the agent called, in order."""
    tool_names = []
    for message in messages:
        if isinstance(message, AIMessage):
            for tool_call in message.tool_calls:
                tool_names.append(tool_call["name"])
    return tool_names


def extract_answer_text(messages):
    """The text of the last AI message ('' if the agent produced none)."""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return result_text(message.content)
    return ""


def count_tokens(messages):
    input_tokens = 0
    output_tokens = 0
    for message in messages:
        if isinstance(message, AIMessage) and message.usage_metadata:
            input_tokens += message.usage_metadata.get("input_tokens", 0)
            output_tokens += message.usage_metadata.get("output_tokens", 0)
    return input_tokens, output_tokens


def score_case(case, tool_names, answer_text):
    """Returns a list of human-readable failures; an empty list means the case passed."""
    failures = []
    lowered_answer = answer_text.lower()
    for required_tool in case.must_call:
        if required_tool not in tool_names:
            failures.append(f"did not call {required_tool}")
    for forbidden_tool in case.must_not_call:
        if forbidden_tool in tool_names:
            failures.append(
                f"called {forbidden_tool}, which it should not have")
    if case.max_tool_calls is not None and len(tool_names) > case.max_tool_calls:
        failures.append(
            f"made {len(tool_names)} tool calls (limit {case.max_tool_calls})")
    for acceptable_phrases in case.must_mention:
        if not any(phrase.lower() in lowered_answer for phrase in acceptable_phrases):
            failures.append(
                f"answer mentions none of: {', '.join(acceptable_phrases)}")
    for forbidden_phrase in case.must_not_mention:
        if forbidden_phrase.lower() in lowered_answer:
            failures.append(f"answer contains '{forbidden_phrase}'")
    return failures


# ------------------------------------------------------------------ running the agent

@contextmanager
def patched_tool_settings(database_path, profile_path):
    """Points every tool at the eval database/profile, then restores the real settings."""
    fixture_settings = SimpleNamespace(
        db_path=Path(database_path), profile_path=Path(profile_path))
    modules = [importlib.import_module(name)
               for name in TOOL_MODULES_USING_SETTINGS]
    original_settings = [module.settings for module in modules]
    for module in modules:
        module.settings = fixture_settings
    try:
        yield
    finally:
        for module, original in zip(modules, original_settings):
            module.settings = original


def eval_tools():
    return [tool for tool in LOCAL_TOOLS if tool.name not in EXCLUDED_TOOL_NAMES]


async def run_case(agent, case, settings):
    config = {"configurable": {"thread_id": uuid.uuid4().hex},
              "recursion_limit": settings.recursion_limit}
    started_at = time.monotonic()
    try:
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=case.prompt)]}, config)
    except Exception as error:   # a crash is a failed case, not a crashed eval
        return CaseResult(case.case_id, False,
                          [f"agent raised {type(error).__name__}: {str(error)[:200]}"],
                          seconds=time.monotonic() - started_at)
    messages = result["messages"]
    tool_names = extract_tool_names(messages)
    answer_text = extract_answer_text(messages)
    input_tokens, output_tokens = count_tokens(messages)
    failures = score_case(case, tool_names, answer_text)
    return CaseResult(case.case_id, not failures, failures, tool_names, answer_text,
                      input_tokens, output_tokens, time.monotonic() - started_at)


async def run_all(agent, cases, settings, repeat=1):
    results = []
    for case in cases:
        for _ in range(repeat):
            results.append(await run_case(agent, case, settings))
    return results


# ------------------------------------------------------------------ reporting

def summarize(results, cases, *, model_name, repeat):
    per_case = []
    for case in cases:
        case_results = [
            result for result in results if result.case_id == case.case_id]
        passes = sum(result.passed for result in case_results)
        failure_notes = sorted({failure for result in case_results
                                for failure in result.failures})
        per_case.append({"case_id": case.case_id, "description": case.description,
                         "passes": passes, "runs": len(case_results),
                         "failures": failure_notes})
    total_runs = len(results)
    total_passes = sum(result.passed for result in results)
    return {
        "date": date.today().isoformat(),
        "model": model_name,
        "runs_per_case": repeat,
        "cases": len(cases),
        "total_runs": total_runs,
        "total_passes": total_passes,
        "pass_rate": round(total_passes / total_runs, 3) if total_runs else 0.0,
        "input_tokens": sum(result.input_tokens for result in results),
        "output_tokens": sum(result.output_tokens for result in results),
        "seconds": round(sum(result.seconds for result in results), 1),
        "per_case": per_case,
    }


def format_report(summary):
    lines = [f"Eval on {summary['date']} with {summary['model']}: "
             f"{summary['total_passes']}/{summary['total_runs']} runs passed "
             f"({summary['pass_rate']:.0%}), {summary['runs_per_case']} run(s) per case.",
             "",
             "| Case | Passed | Notes |", "|---|---|---|"]
    for case_summary in summary["per_case"]:
        notes = "; ".join(case_summary["failures"]) or ""
        lines.append(f"| {case_summary['case_id']} | "
                     f"{case_summary['passes']}/{case_summary['runs']} | {notes} |")
    lines += ["", f"Tokens: {summary['input_tokens']} in, {summary['output_tokens']} out. "
              f"Wall time: {summary['seconds']}s."]
    return "\n".join(lines)


# ------------------------------------------------------------------ command line

def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Evaluate the bid assistant agent.")
    parser.add_argument("--repeat", type=int, default=1,
                        help="runs per case (the model is not deterministic; 3 is a good value)")
    parser.add_argument("--only", default=None,
                        help="run a single case by its id")
    parser.add_argument("--model", default=None,
                        help="override the model name")
    parser.add_argument("--output", default="evals/results/latest.json",
                        help="where to write the JSON results ('' to skip)")
    parser.add_argument("--fail-under", type=float, default=0.0,
                        help="exit with status 1 if the overall pass rate is below this (0-1)")
    arguments = parser.parse_args(argv)
    if arguments.repeat < 1:
        parser.error("--repeat must be at least 1")
    return arguments


async def run_evaluation(arguments):
    settings = Settings()
    if arguments.model:
        settings = replace(settings, model=arguments.model)
    cases = [case for case in EVAL_CASES
             if arguments.only in (None, case.case_id)]
    if not cases:
        raise SystemExit(f"no case named {arguments.only!r}")
    system_prompt = settings.prompt_path.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as fixture_directory:
        database_path, profile_path = build_fixture(fixture_directory)
        with patched_tool_settings(database_path, profile_path):
            agent = build_agent(eval_tools(), settings, checkpointer=InMemorySaver(),
                                system_prompt=system_prompt, name="eval-agent")
            results = await run_all(agent, cases, settings, repeat=arguments.repeat)

    summary = summarize(
        results, cases, model_name=settings.model, repeat=arguments.repeat)
    print(format_report(summary))
    if arguments.output:
        output_path = Path(arguments.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nWrote {output_path}")
    return summary


def main(argv=None):
    arguments = parse_arguments(argv)
    summary = asyncio.run(run_evaluation(arguments))
    if summary["pass_rate"] < arguments.fail_under:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
