import re
from pydantic import BaseModel, field_validator


class UserQuery(BaseModel):
    query: str

    @field_validator("query")
    @classmethod
    def validate_query(cls, v):
        if len(v) > 1000:
            raise ValueError("Query exceeds limit")
        return v.strip()


INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\s+(?:(?:all|any|the|your|my|previous|prior|above|earlier)\s+)*(?:instructions|rules|prompts?)\b",
    r"\b(reveal|show|print|repeat)\s+(?:your\s+)?system\s+prompt\b",
    r"\byou\s+are\s+now\b",
    r"\bact\s+as\s+(?:if\s+)?you\b",
]


def detect_injection(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in INJECTION_PATTERNS)


SENSITIVE_PATTERNS = [
    (r"[\w.+-]+@[\w-]+\.[\w.-]+", "[EMAIL REDACTED]"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN REDACTED]"),
    (r"sk-[A-Za-z0-9_-]{20,}", "[API KEY REDACTED]"),
]


def filter_output(text: str) -> str:
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text
