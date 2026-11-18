"""Try the agent on SYNTHETIC data, with no SAM.gov data or database of your own.

    python -m evals.demo
    python -m evals.demo --question "Find open notices in NAICS 541512 and score the best one."

This builds the same invented database the eval uses (seven made-up notices, none of them real
SAM.gov opportunities), points the tools at it, and starts the normal agent. It needs only
ANTHROPIC_API_KEY. The write tool (refresh_opportunities) is left out.
"""
import argparse
import asyncio
import tempfile
import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

from evals.fixture import build_fixture
from evals.run_evals import eval_tools, patched_tool_settings
from src.agent import build_agent, run_turn
from src.settings import Settings

SUGGESTED_QUESTIONS = (
    "Find open notices in NAICS 541512 and score the best one.",
    "Which notices have attachments that mention cloud migration?",
    "Score notice EVAL-NAICS-002 for us.",
    "Score notice EVAL-NODOCS-006 and tell me what its documents require.",
)

BANNER = """\
DEMO MODE: every notice in this database is invented. None are real SAM.gov opportunities.
Try one of these questions (or your own). Type 'quit' to quit.
""" + "\n".join(f"  - {question}" for question in SUGGESTED_QUESTIONS)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the agent on synthetic demo data.")
    parser.add_argument("--question", default=None,
                        help="ask one question and exit instead of starting an interactive session")
    return parser.parse_args(argv)


async def run_demo(arguments):
    settings = Settings()
    system_prompt = settings.prompt_path.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as fixture_directory:
        database_path, profile_path = build_fixture(fixture_directory)
        with patched_tool_settings(database_path, profile_path):
            agent = build_agent(eval_tools(), settings, checkpointer=InMemorySaver(),
                                system_prompt=system_prompt, name="demo-agent")
            config = {"configurable": {"thread_id": uuid.uuid4().hex},
                      "recursion_limit": settings.recursion_limit}
            if arguments.question:
                print(f"You: {arguments.question}")
                print(await run_turn(agent, arguments.question, config))
                return
            print(BANNER)
            while True:
                question = input("You: ")
                if question.strip().lower() == "quit":
                    break
                try:
                    print(await run_turn(agent, question, config))
                except GraphRecursionError:
                    print("Error: the agent hit its step limit without finishing.")


def main(argv=None):
    asyncio.run(run_demo(parse_arguments(argv)))


if __name__ == "__main__":
    main()
