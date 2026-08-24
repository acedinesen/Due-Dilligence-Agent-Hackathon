from __future__ import annotations

from typing import Protocol

from app.models import (
    DiligenceReport,
    Finding,
    FirmProfile,
    FounderQuestion,
    MetricResult,
    ParsedDeck,
    Source,
    SourceType,
)


class DiligenceAgent(Protocol):
    async def analyze(self, deck: ParsedDeck, firm: FirmProfile | None) -> DiligenceReport:
        ...


class MockDiligenceAgent:
    """
    Keeps the API runnable while Claude + research tools are wired in.
    It intentionally does not invent external evidence.
    """

    async def analyze(self, deck: ParsedDeck, firm: FirmProfile | None) -> DiligenceReport:
        deck_source = Source(
            type=SourceType.DECK,
            title=deck.filename,
            evidence="The pitch deck was parsed successfully.",
            page=deck.pages[0].page if deck.pages else 1,
        )

        return DiligenceReport(
            company_name=deck.filename.rsplit(".", 1)[0],
            overview="Deck ingestion works. Connect Claude research to generate real diligence.",
            metrics=[
                MetricResult(name="tam", status="unknown", summary="Not researched yet.", sources=[deck_source]),
                MetricResult(name="competitors", status="unknown", summary="Not researched yet.", sources=[deck_source]),
                MetricResult(name="founder", status="unknown", summary="Not researched yet.", sources=[deck_source]),
            ],
            key_findings=[
                Finding(
                    id="research-not-run",
                    title="External diligence has not run",
                    risk_level="medium",
                    explanation="The base is currently using the mock diligence agent.",
                    why_it_matters="TAM, competitors and founder claims still need external validation.",
                    sources=[deck_source],
                )
            ],
            founder_questions=[
                FounderQuestion(
                    question="Which assumption in this deck has the weakest external evidence today?",
                    based_on_finding_ids=["research-not-run"],
                )
            ],
        )
