import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _model() -> str:
    return os.getenv("AGENTMODEL", "claude-sonnet-5")


@dataclass(frozen=True)
class Settings:
    model: str = field(default_factory=_model)
    run_limit: int = 10
    recursion_limit: int = 50
    max_input_chars: int = 1000
    checkpointer: str = "memory"
    sqlite_path: str = "checkpoints.db"
    prompt_path: Path = Path(__file__).parent / "prompts" / "main.md"
    mcp_servers: dict = field(default_factory=dict)

    def __post_init__(self):
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set (see .env.example)")
