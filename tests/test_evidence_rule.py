"""Tests for code-side enforcement of the Evidence Rule (docs/BUILD_PLAN.md §5).

The rule is that every external claim carries a source URL that a search
actually returned. The pydantic model can only check that a URL is *present*
and well-formed, so the "and it is real" half lives in `_uncited_urls` — which
makes it the piece worth testing, because a regression here does not raise, it
silently ships a confident false citation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.diligence import _uncited_urls, _url_key
from app.models import DiligenceReport, ParsedDeck, Source, SourceType

FIXTURES = Path(__file__).parent / "fixtures"

# A real profile URL that a search returned, whose slug is deliberately not
# derivable from the person's name — so a fabricated URL cannot coincide with it.
HARVESTED_PROFILE = "https://www.linkedin.com/in/dkfox/"


def _load_report() -> DiligenceReport:
    return DiligenceReport.model_validate(json.loads((FIXTURES / "sample_report.json").read_text()))


def _load_deck(name: str) -> ParsedDeck:
    return ParsedDeck.model_validate(json.loads((FIXTURES / name).read_text()))


@pytest.fixture
def deck() -> ParsedDeck:
    """A deck that prints no URL anywhere."""
    return _load_deck("sample_deck.json")


@pytest.fixture
def deck_with_website() -> ParsedDeck:
    """A deck that prints its own address, schemeless, as real decks do."""
    return _load_deck("sample_deck_findable.json")


@pytest.fixture
def bare_report() -> DiligenceReport:
    """The real report with every URL stripped, so each test adds only its own."""
    report = _load_report()
    for section in (report.tam_sam_som, report.competitors, report.founder_profile):
        section.sources = []
    for metric in report.additional_metrics:
        metric.sources = []
    for finding in report.key_findings:
        finding.sources = []
    report.company.website_url = None
    report.founders = []
    return report


def _external(url: str) -> Source:
    return Source(type=SourceType.EXTERNAL, title="t", evidence="e", url=url)


class TestUrlKey:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("https://www.example.com/x/", "http://example.com/x"),
            ("https://EXAMPLE.com/X", "https://example.com/X"),
            ("https://example.com", "example.com/"),
        ],
    )
    def test_treats_cosmetic_variants_as_the_same_url(self, a: str, b: str) -> None:
        # HttpUrl normalises, and a model may add or drop "www."; comparing raw
        # strings would reject URLs that are genuinely the harvested one.
        assert _url_key(a) == _url_key(b)

    def test_distinguishes_different_paths_on_one_host(self) -> None:
        assert _url_key("https://linkedin.com/in/a") != _url_key("https://linkedin.com/in/b")


class TestExternalEvidence:
    def test_harvested_url_is_accepted(self, bare_report: DiligenceReport, deck: ParsedDeck) -> None:
        bare_report.key_findings[0].sources = [_external("https://example.com/report")]
        assert _uncited_urls(bare_report, [("t", "https://example.com/report")], deck) == []

    def test_unharvested_url_is_reported(
        self, bare_report: DiligenceReport, deck: ParsedDeck
    ) -> None:
        bare_report.key_findings[0].sources = [_external("https://invented.example/a")]
        offenders = _uncited_urls(bare_report, [("t", "https://example.com/report")], deck)
        assert len(offenders) == 1
        assert "invented.example" in offenders[0]
        # The location is named, so the correction prompt can point at the field.
        assert bare_report.key_findings[0].id in offenders[0]

    def test_a_deck_printed_url_does_not_license_external_evidence(
        self, bare_report: DiligenceReport, deck_with_website: ParsedDeck
    ) -> None:
        # The deck prints performativ.com, but external evidence must come from
        # research — the deck asserting something is not third-party validation.
        bare_report.key_findings[0].sources = [_external("https://www.performativ.com/pricing")]
        assert _uncited_urls(bare_report, [], deck_with_website) != []

    def test_deck_sources_are_ignored_by_the_url_check(
        self, bare_report: DiligenceReport, deck: ParsedDeck
    ) -> None:
        # A deck source carries a page, not a URL, so it is out of scope here.
        bare_report.key_findings[0].sources = [
            Source(type=SourceType.DECK, title="deck", evidence="e", page=3)
        ]
        assert _uncited_urls(bare_report, [], deck) == []

    def test_every_section_is_walked(self, bare_report: DiligenceReport, deck: ParsedDeck) -> None:
        # A section left unchecked is a hole in the rule, so pin all of them.
        bad = "https://invented.example/x"
        bare_report.tam_sam_som.sources = [_external(bad)]
        bare_report.competitors.sources = [_external(bad)]
        bare_report.founder_profile.sources = [_external(bad)]
        bare_report.additional_metrics[0].sources = [_external(bad)]
        bare_report.key_findings[0].sources = [_external(bad)]
        assert len(_uncited_urls(bare_report, [], deck)) == 5


class TestWebsiteUrl:
    def test_accepted_when_the_deck_prints_it_schemeless(
        self, bare_report: DiligenceReport, deck_with_website: ParsedDeck
    ) -> None:
        # The deck says "www.performativ.com" with no scheme. Reading a URL off
        # the deck is evidence, not a guess, so this must not be rejected.
        bare_report.company.website_url = "https://www.performativ.com/"
        assert _uncited_urls(bare_report, [], deck_with_website) == []

    def test_rejected_when_neither_the_deck_nor_a_search_has_it(
        self, bare_report: DiligenceReport, deck: ParsedDeck
    ) -> None:
        bare_report.company.website_url = "https://www.performativ.com/"
        assert _uncited_urls(bare_report, [], deck) != []

    def test_matches_on_host_so_a_deeper_page_is_allowed(
        self, bare_report: DiligenceReport, deck: ParsedDeck
    ) -> None:
        bare_report.company.website_url = "https://acme.example/about/team"
        assert _uncited_urls(bare_report, [("t", "https://acme.example/")], deck) == []

    def test_none_is_never_an_offender(
        self, bare_report: DiligenceReport, deck: ParsedDeck
    ) -> None:
        # Returning None is the correct answer for an unfindable company, so it
        # must never be penalised.
        bare_report.company.website_url = None
        assert _uncited_urls(bare_report, [], deck) == []


class TestFounderLinkedIn:
    def _with_profile(self, report: DiligenceReport, url: str | None) -> DiligenceReport:
        report.founders = [
            report.founders[0].model_copy(update={"linkedin_url": url})
            if report.founders
            else _load_report().founders[0].model_copy(update={"linkedin_url": url})
        ]
        return report

    def test_harvested_profile_is_accepted(
        self, bare_report: DiligenceReport, deck: ParsedDeck
    ) -> None:
        report = self._with_profile(bare_report, HARVESTED_PROFILE)
        assert _uncited_urls(report, [("t", HARVESTED_PROFILE)], deck) == []

    def test_a_guessed_slug_on_the_same_host_is_rejected(
        self, bare_report: DiligenceReport, deck: ParsedDeck
    ) -> None:
        # The failure mode this exists for: the real profile is /in/dkfox, which
        # cannot be derived from the name, so a model that fabricates rather than
        # researches emits a name-shaped slug. Host-level matching would wave it
        # through and attach the wrong person to the CRM record.
        report = self._with_profile(bare_report, "https://linkedin.com/in/albert-geisler-fox")
        assert _uncited_urls(report, [("t", HARVESTED_PROFILE)], deck) != []

    def test_none_is_never_an_offender(
        self, bare_report: DiligenceReport, deck: ParsedDeck
    ) -> None:
        report = self._with_profile(bare_report, None)
        assert _uncited_urls(report, [], deck) == []


def test_the_real_agent_output_passes_against_its_own_harvested_urls(
    deck: ParsedDeck,
) -> None:
    """Guards against the check being too strict to be usable.

    `sample_report.json` is a real agent run, so every URL in it was genuinely
    harvested. If this fails, the check would reject correct reports — a worse
    outcome than the gap it closes, because it would fail every deck.
    """
    report = _load_report()
    harvested = [
        ("t", "https://www.seedtable.com/best-fintech-startups-in-copenhagen"),
        ("t", "https://www.cbinsights.com/research/payhawk-competitors-spendesk-pleo-moss-soldo/"),
        ("t", "https://prifinance.com/en/payments/emi-license-denmark/"),
        ("t", "https://www.linkedin.com/in/mikjo/"),
        ("t", "https://www.linkedin.com/in/aino-lehtinen-43a649183/"),
    ]
    assert _uncited_urls(report, harvested, deck) == []
