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


class MetricResult(BaseModel):
    name: Literal["tam", "competitors", "founder"]
    status: Literal["supported", "questionable", "red_flag", "unknown"]
    summary: str
    sources: list[Source] = Field(default_factory=list)


class Finding(BaseModel):
    id: str
    title: str
    risk_level: Literal["low", "medium", "high"]
    explanation: str
    why_it_matters: str
    sources: list[Source] = Field(default_factory=list)


class FounderQuestion(BaseModel):
    question: str
    based_on_finding_ids: list[str] = Field(default_factory=list)


class DiligenceReport(BaseModel):
    company_name: str | None = None
    overview: str
    metrics: list[MetricResult]
    key_findings: list[Finding] = Field(max_length=5)
    founder_questions: list[FounderQuestion] = Field(max_length=5)


class FirmProfile(BaseModel):
    id: str
    name: str
    criteria: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    flag: Literal["relevant", "review", "not_relevant"]
    reason: str
