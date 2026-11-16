from enum import Enum


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


ACTION_RISK: dict[str, RiskLevel] = {
    "search_opportunities": RiskLevel.LOW,
    "get_opportunity": RiskLevel.LOW,
    "search_solicitation_documents": RiskLevel.LOW,
    "score_opportunity": RiskLevel.LOW
}


def needs_human_approval(tool_name: str) -> bool:
    return ACTION_RISK.get(tool_name, RiskLevel.HIGH) is not RiskLevel.LOW


def interrupt_map(tools) -> dict[str, bool]:
    return {t.name: True for t in tools if needs_human_approval(t.name)}
