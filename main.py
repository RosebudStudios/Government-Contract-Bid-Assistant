import asyncio
from langchain_anthropic import ChatAnthropic
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware, ModelRetryMiddleware, ToolRetryMiddleware, HumanInTheLoopMiddleware, PIIMiddleware, before_agent
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import AIMessage
from input_guards import (detect_injection)

model = ChatAnthropic(model_name="claude-sonnet-5", timeout=None, stop=None)
checkpointer = InMemorySaver()
tools = []


@before_agent(can_jump_to=["end"])
def input_guard(state: AgentState, runtime):
    text = state["messages"][-1].content
    if detect_injection(text):
        return {"messages": [AIMessage(content="Blocked: injection")],
                "jump_to": "end"}
    return None


pii_types = ["email", "credit_card", "ip", "mac_address"]
pii_middleware = [
    PIIMiddleware(t, strategy="redact", apply_to_output=True)
    for t in pii_types
]
middleware = [*pii_middleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware,
              ModelRetryMiddleware, ToolRetryMiddleware, HumanInTheLoopMiddleware, input_guard]

main_agent = create_agent(
    model=model,
    checkpointer=checkpointer,
    middleware=middleware,
    tools=tools

)
