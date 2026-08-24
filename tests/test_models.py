import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import (
    CompanySummary,
    CompetitorAnalysis,
    Competitor,
    DiligenceReport,
    Finding,
    FounderCategoryNote,
    FounderProfile,
    FounderQuestion,
    FounderSummary,
    MetricResult,
    ParsedDeck,
    Source,
    SourceType,
    TamSamSomBreakdown,
)

FIXTURE_DECK = Path(__file__).parent / "fixtures" / "sample_deck.json"


def test_external_evidence_requires_url():
    with pytest.raises(ValidationError):
        Source(
            type=SourceType.EXTERNAL,
            title="Market report",
            evidence="Supports the TAM claim.",
        )


def test_deck_evidence_requires_page():
    with pytest.raises(ValidationError):
        Source(
            type=SourceType.DECK,
            title="Pitch deck",
            evidence="Deck claim.",
        )


def _deck_source() -> Source:
    return Source(
        type=SourceType.DECK,
        title="thalvik-seed-deck.pdf",
        evidence="Page 5 states TAM USD 48B, SAM USD 6.2B, SOM USD 2.9B.",
        page=5,
    )


def _finding(index: int) -> Finding:
    return Finding(
        id=f"finding-{index}",
        title=f"Material issue {index}",
        risk_level="medium",
        pillar="tam",
        explanation="The stated SOM is 47% of the stated SAM.",
        why_it_matters="A SOM that large implies near-total share capture in three years.",
        sources=[_deck_source()],
    )


def _report(
    key_findings: list[Finding] | None = None,
    founder_questions: list[FounderQuestion] | None = None,
) -> DiligenceReport:
    deck_source = _deck_source()
    return DiligenceReport(
        company=CompanySummary(
            name="Thalvik",
            one_liner="Spend management that reconciles card and invoice spend into the ledger continuously.",
            website_url=None,
        ),
        overview="Seed-stage Copenhagen spend-management company with EUR 41.3k MRR and 34 customers.",
        tam_sam_som=TamSamSomBreakdown(
            tam_stated="USD 48B",
            tam_methodology="top_down",
            sam_stated="USD 6.2B",
            som_stated="USD 2.9B",
            som_pct_of_sam_flagged=True,
            external_validation_present=False,
            summary="SOM is ~47% of SAM, far outside the credible range.",
            sources=[deck_source],
        ),
        competitors=CompetitorAnalysis(
            competitors=[
                Competitor(
                    name="SAP Concur",
                    funding_info="Acquired by SAP in 2014 for USD 8.3B.",
                    differentiation_claimed="Enterprise-only pricing and 6-9 month implementations.",
                    is_direct=False,
                    verified_externally=True,
                )
            ],
            why_now_why_us="E-invoicing mandates force an AP stack change in the next 24 months.",
            missing_direct_competitor_flag=True,
            summary="The competition slide omits Pleo and Spendesk, the obvious direct competitors.",
            sources=[deck_source],
        ),
        founder_profile=FounderProfile(
            categories=[
                FounderCategoryNote(
                    category="industry_experience",
                    status="supported",
                    evidence="CEO ran the monthly close for a division of a large shipping group.",
                ),
                FounderCategoryNote(
                    category="track_record",
                    status="unknown",
                    evidence="No prior founding experience is claimed or found.",
                ),
            ],
            founder_market_fit="strong",
            summary="Controller-plus-payments-engineer pairing maps directly onto the wedge.",
            sources=[deck_source],
        ),
        founders=[
            FounderSummary(
                name="Mikkel Ravnsborg",
                bio_one_liner="Former Head of Finance Operations; ran divisional close at a global shipping group.",
                linkedin_url=None,
            )
        ],
        additional_metrics=[
            MetricResult(
                name="problem_validation",
                status="supported",
                summary="40 discovery interviews with named buyer personas.",
                sources=[deck_source],
            ),
            MetricResult(
                name="cap_table_legal",
                status="red_flag",
                summary="An advisor holds 12% of common stock from a pre-counsel 2024 arrangement.",
                sources=[deck_source],
            ),
        ],
        key_findings=[_finding(1)] if key_findings is None else key_findings,
        founder_questions=(
            [
                FounderQuestion(
                    question="Your SOM is 47% of your SAM by year three — which specific accounts get you there?",
                    based_on_finding_ids=["finding-1"],
                )
            ]
            if founder_questions is None
            else founder_questions
        ),
    )


def test_full_diligence_report_validates():
    report = _report()

    assert DiligenceReport.model_validate(report.model_dump()) == report
    assert report.company.website_url is None
    assert report.tam_sam_som.som_pct_of_sam_flagged is True
    assert report.competitors.missing_direct_competitor_flag is True
    assert [m.name for m in report.additional_metrics] == [
        "problem_validation",
        "cap_table_legal",
    ]


def test_report_allows_exactly_five_key_findings():
    report = _report(key_findings=[_finding(i) for i in range(1, 6)])

    assert len(report.key_findings) == 5


def test_report_rejects_more_than_five_key_findings():
    with pytest.raises(ValidationError):
        _report(key_findings=[_finding(i) for i in range(1, 7)])


def test_report_rejects_more_than_five_founder_questions():
    questions = [
        FounderQuestion(question=f"Question {i}?", based_on_finding_ids=["finding-1"])
        for i in range(6)
    ]

    with pytest.raises(ValidationError):
        _report(founder_questions=questions)


def test_sample_deck_fixture_is_a_valid_parsed_deck():
    deck = ParsedDeck.model_validate(json.loads(FIXTURE_DECK.read_text(encoding="utf-8")))

    assert deck.filename == "thalvik-seed-deck.pdf"
    assert len(deck.pages) >= 8
    assert [p.page for p in deck.pages] == list(range(1, len(deck.pages) + 1))
    assert deck.full_text == "\n\n".join(p.text for p in deck.pages)
