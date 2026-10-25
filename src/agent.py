import uuid
import asyncio
from contextlib import asynccontextmanager

import anthropic
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware, ModelCallLimitMiddleware, ModelRetryMiddleware,
    PIIMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware, before_agent,
)
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command


from .guards import UserQuery, detect_injection
from .settings import Settings, require_env
from .tools import interrupt_map, load_tools


RETRYABLE = (anthropic.APIConnectionError, anthropic.APITimeoutError,
             anthropic.RateLimitError, anthropic.InternalServerError)


def ask_approval(name: str, args: dict) -> bool:
    answer = input(f"Allow {name} with {args}? (y/n): ")
    return answer.strip().lower() in ("y", "yes")


def make_input_guard(max_chars: int):
    @before_agent(can_jump_to=["end"])
    def input_guard(state: AgentState, runtime):
        content = state["messages"][-1].content
        text = content if isinstance(content, str) else ""
        if len(text) > max_chars:
            return {"messages": [AIMessage(content=f"Request blocked: input is longer than {max_chars} characters")], "jump_to": "end"}
        if detect_injection(UserQuery(query=text).query):
            return {"messages": [AIMessage(content="I can't follow that request")],
                    "jump_to": "end"}
        return None


@asynccontextmanager
async def open_checkpointer(settings: Settings):
    if settings.checkpointer == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        async with AsyncSqliteSaver.from_conn_string(settings.sqlite_path) as saver:
            yield saver
    else:
        yield InMemorySaver()


def build_agent(tools, settings: Settings, *, checkpointer, model=None, system_prompt=None, name="agent"):
    if model is None:
        model = ChatAnthropic(model_name=settings.model,
                              timeout=None, stop=None)
        pii = [PIIMiddleware(t, strategy="redact", apply_to_output=True)
               for t in ["email", "credit_card", "ip", "mac_address"]]
        pii += [
            PIIMiddleware("ssn", detector=r"\b\d{3}-\d{2}-\d{4}\b",
                          strategy="redact", apply_to_output=True),
            PIIMiddleware("api_key", detector=r"sk-[A-Za-z0-9_-]{20,}",
                          strategy="redact", apply_to_output=True),
        ]
        middleware = [
            make_input_guard(settings.max_input_chars), *pii,
            ModelCallLimitMiddleware(run_limit=settings.run_limit),
            ToolCallLimitMiddleware(run_limit=settings.run_limit),
            ModelRetryMiddleware(max_retries=3, retry_on=RETRYABLE),
            ToolRetryMiddleware(max_retries=2),
        ]
        if gated := interrupt_map(tools):
            middleware.append(HumanInTheLoopMiddleware(interrupt_on=gated))
        return create_agent(model=model, tools=tools, system_prompt=system_prompt,
                            middleware=middleware, checkpointer=checkpointer, name=name)


def result_text(result):
    if isinstance(result, list):
        return "\n".join(b.get("text", "") for b in result
                         if isinstance(b, dict) and b.get("type") == "text")
    return str(result)


async def run_turn(agent, query, config, approve=ask_approval):
    payload = {"messages": [HumanMessage(content=query)]}
    while True:
        result = await agent.ainvoke(payload, config)
        interrupts = result.get("__interrupt__")
        if not interrupts:
            return result_text(result["messages"][-1].content)
        decisions = [
            {"type": "approve"} if approve(r["name"], r["args"])
            else {"type": "reject", "message": "The user denied this action."}
            for r in interrupts[0].value["action_requests"]
        ]

        payload = Command(resume={"decisions": decisions})


async def main():
    settings = Settings()
    tools = await load_tools(settings)
    agent = build_agent(tools=[])
    config = {"configurable": {"thread_id": uuid.uuid4().hex},
              "recursion_limit": 50}
    print("Enter your question. Type 'quit' to quit.")
    while True:
        question = input("You: ")
        if question.lower() == 'quit':
            break
        try:
            print(await run_turn(agent, question, config))
        except GraphRecursionError:
            print("Error: the agent hit its step limit without finishing.")


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
