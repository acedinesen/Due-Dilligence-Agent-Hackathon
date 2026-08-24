from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class DeckPage(BaseModel):
    page: int
    text: str


class ParsedDeck(BaseModel):
    filename: str
    full_text: str
    pages: list[DeckPage] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class SourceType(str, Enum):
    DECK = "deck"
    EXTERNAL = "external"


class Source(BaseModel):
    type: SourceType
    title: str
    evidence: str
    page: int | None = None
    url: HttpUrl | None = None

    @model_validator(mode="after")
    def external_sources_need_links(self):
        if self.type == SourceType.EXTERNAL and self.url is None:
            raise ValueError("Every external source must include a URL")
        if self.type == SourceType.DECK and self.page is None:
            raise ValueError("Deck evidence must include a page number")
        return self


class TamSamSomBreakdown(BaseModel):
    tam_stated: str | None = None
    tam_methodology: Literal["top_down", "bottom_up", "both", "unclear"] = "unclear"
    sam_stated: str | None = None
    som_stated: str | None = None
    som_pct_of_sam_flagged: bool = False  # True if outside the credible ~1-15% SOM/SAM range, or not derivable
    external_validation_present: bool = False
    summary: str
    sources: list[Source] = Field(default_factory=list)


class Competitor(BaseModel):
    name: str
    funding_info: str | None = None
    differentiation_claimed: str
    is_direct: bool
    verified_externally: bool


class CompetitorAnalysis(BaseModel):
    competitors: list[Competitor] = Field(default_factory=list)
    why_now_why_us: str | None = None
    missing_direct_competitor_flag: bool = False
    summary: str
    sources: list[Source] = Field(default_factory=list)


FounderCategory = Literal[
    "industry_experience", "vision_strategy", "track_record",
    "learning_agility", "team_leadership", "network_strength",
    "resilience", "execution_strength",
]


class FounderCategoryNote(BaseModel):
    category: FounderCategory
    status: Literal["supported", "questionable", "red_flag", "unknown"]
    evidence: str


class FounderProfile(BaseModel):
    categories: list[FounderCategoryNote]
    founder_market_fit: Literal["strong", "moderate", "weak", "unclear"]
    summary: str
    sources: list[Source] = Field(default_factory=list)


class MetricResult(BaseModel):
    name: Literal[
        "problem_validation", "traction", "business_model_clarity",
        "cap_table_legal", "ask_and_use_of_funds", "non_obvious_insight",
    ]
    status: Literal["supported", "questionable", "red_flag", "unknown"]
    summary: str
    sources: list[Source] = Field(default_factory=list)


class Finding(BaseModel):
    id: str
    title: str
    risk_level: Literal["low", "medium", "high"]
    pillar: Literal[
        "tam", "competitors", "founder", "traction",
        "business_model", "legal", "ask", "other",
    ] | None = None
    explanation: str
    why_it_matters: str
    sources: list[Source] = Field(default_factory=list)


class FounderQuestion(BaseModel):
    question: str
    based_on_finding_ids: list[str] = Field(default_factory=list)


class CompanySummary(BaseModel):
    name: str
    one_liner: str
    website_url: HttpUrl | None = None


class FounderSummary(BaseModel):
    name: str
    bio_one_liner: str
    linkedin_url: HttpUrl | None = None


class DiligenceReport(BaseModel):
    company: CompanySummary
    overview: str
    tam_sam_som: TamSamSomBreakdown
    competitors: CompetitorAnalysis
    founder_profile: FounderProfile
    founders: list[FounderSummary] = Field(default_factory=list)
    additional_metrics: list[MetricResult] = Field(default_factory=list)
    key_findings: list[Finding] = Field(max_length=5)
    founder_questions: list[FounderQuestion] = Field(max_length=5)


class FirmProfile(BaseModel):
    id: str
    name: str
    criteria: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    flag: Literal["relevant", "review", "not_relevant"]
    reason: str
