import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _model() -> str:
    return os.getenv("AGENT_MODEL", "claude-sonnet-5")


@dataclass(frozen=True)
class Settings:
    model: str = field(default_factory=_model)
    run_limit: int = 10
    recursion_limit: int = 200
    max_input_chars: int = 1000
    checkpointer: str = "memory"
    sqlite_path: str = "checkpoints.db"
    prompt_path: Path = Path(__file__).parent / "prompts" / "main.md"
    db_path: Path = Path(__file__).parent.parent / "data" / "solicitations.db"
    documents_dir: Path = Path(__file__).parent.parent/"data"/"documents"
    profile_path: Path = Path(__file__).parent.parent / \
        "config" / "company_profile.toml"
    mcp_servers: dict = field(default_factory=dict)
    sam_base_url: str = "https://api.sam.gov/opportunities/v2/search"
    sync_lookback_days: int = 1
    daily_request_limit: int = 10
    description_requests_per_run: int = 5
    target_naics_codes: tuple = ("541512", "541511")
    attachment_requests_count_toward_quota: bool = False
    max_attachment_downloads_per_run: int = 10
    max_attachment_bytes: int = 25 * 1024 * 1024


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set (see .env.example)")
    return value
