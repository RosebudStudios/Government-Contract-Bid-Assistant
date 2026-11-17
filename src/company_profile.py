from pydantic import BaseModel, Field, ConfigDict


class CompanyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    naics_codes: list[str] = Field(default_factory=list)
    set_aside_eligibilities: list[str] = Field(default_factory=list)
    states_served: list[str] = Field(default_factory=list)
    notice_types: list[str] = Field(default_factory=list)
    minimum_days_to_respond: int = 0

    capabilities: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    summary: str = ""
