from __future__ import annotations

from typing import Protocol

from app.models import (
    CompanySummary,
    CompetitorAnalysis,
    DiligenceReport,
    Finding,
    FirmProfile,
    FounderCategoryNote,
    FounderProfile,
    FounderQuestion,
    MetricResult,
    ParsedDeck,
    Source,
    SourceType,
    TamSamSomBreakdown,
)


class DiligenceAgent(Protocol):
    async def analyze(self, deck: ParsedDeck, firm: FirmProfile | None) -> DiligenceReport:
        ...


class MockDiligenceAgent:
    """
    Keeps the API runnable while Claude + research tools are wired in.
    It exists only to prove the plumbing — it is a placeholder and
    intentionally does not invent external evidence.
    """

    async def analyze(self, deck: ParsedDeck, firm: FirmProfile | None) -> DiligenceReport:
        deck_source = Source(
            type=SourceType.DECK,
            title=deck.filename,
            evidence="The pitch deck was parsed successfully.",
            page=deck.pages[0].page if deck.pages else 1,
        )

        return DiligenceReport(
            company=CompanySummary(
                name=deck.filename.rsplit(".", 1)[0],
                one_liner="Placeholder one-liner — no research has run for this deck yet.",
                website_url=None,
            ),
            overview="Deck ingestion works. Connect Claude research to generate real diligence.",
            tam_sam_som=TamSamSomBreakdown(
                summary="Market sizing has not been researched yet.",
                sources=[deck_source],
            ),
            competitors=CompetitorAnalysis(
                summary="Competitive landscape has not been researched yet.",
                sources=[deck_source],
            ),
            founder_profile=FounderProfile(
                categories=[
                    FounderCategoryNote(
                        category="industry_experience",
                        status="unknown",
                        evidence="No external founder research has run yet.",
                    )
                ],
                founder_market_fit="unclear",
                summary="Founder background has not been researched yet.",
                sources=[deck_source],
            ),
            founders=[],
            additional_metrics=[
                MetricResult(
                    name="problem_validation",
                    status="unknown",
                    summary="Not researched yet.",
                    sources=[deck_source],
                ),
                MetricResult(
                    name="traction",
                    status="unknown",
                    summary="Not researched yet.",
                    sources=[deck_source],
                ),
                MetricResult(
                    name="business_model_clarity",
                    status="unknown",
                    summary="Not researched yet.",
                    sources=[deck_source],
                ),
            ],
            key_findings=[
                Finding(
                    id="research-not-run",
                    title="External diligence has not run",
                    risk_level="medium",
                    pillar="other",
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
