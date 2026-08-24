from __future__ import annotations

import anthropic

from app.config import settings
from app.models import FirmProfile, ParsedDeck, TriageResult

TRIAGE_MODEL = "claude-sonnet-5"


class TriageAgent:
    """Cheap, fast pre-filter that runs on every inbound deck.

    This is NOT the deep-research agent (that's Track B) — one Claude call,
    no web search, no multi-turn loop. It only judges fit against the firm's
    stated criteria and basic deck credibility.
    """

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def classify(self, deck: ParsedDeck, firm: FirmProfile | None) -> TriageResult:
        prompt = self._build_prompt(deck, firm)
        model = settings.anthropic_model or TRIAGE_MODEL

        response = await self._client.messages.parse(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            output_format=TriageResult,
        )
        return response.parsed_output

    def _build_prompt(self, deck: ParsedDeck, firm: FirmProfile | None) -> str:
        criteria_block = ""
        if firm and firm.criteria:
            bullet_list = "\n".join(f"- {criterion}" for criterion in firm.criteria)
            criteria_block = f"\n\nThe firm's investment criteria:\n{bullet_list}"

        return (
            "You are a fast triage pre-filter for a VC associate's inbox, not a "
            "deep-research analyst. Do NOT perform real research or invent external "
            "facts — judge only from what's in the deck text below, plus basic "
            "internal credibility (e.g. internally inconsistent numbers, missing "
            "team/product/market info, obvious spam or non-pitch-deck content).\n"
            f"{criteria_block}\n\n"
            "Classify this pitch deck as one of:\n"
            "- relevant: clearly fits the firm's criteria and looks credible\n"
            "- review: plausible but unclear fit, or missing info a human should judge\n"
            "- not_relevant: clearly out of scope, low quality, or not a pitch deck\n\n"
            f"Deck filename: {deck.filename}\n\n"
            "Deck text:\n"
            f"{deck.full_text}"
        )
