import re
from pydantic import BaseModel


class UserQuery(BaseModel):
    query: str


INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\s+(?:(?:all|any|the|your|my|previous|prior|above|earlier)\s+)*(?:instructions|rules|prompts?)\b",
    r"\b(reveal|show|print|repeat)\s+(?:your\s+)?system\s+prompt\b",
    r"\byou\s+are\s+now\b",
    r"\bact\s+as\s+(?:if\s+)?you\b",
]


def detect_injection(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in INJECTION_PATTERNS)
