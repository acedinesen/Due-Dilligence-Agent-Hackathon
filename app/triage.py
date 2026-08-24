from __future__ import annotations

import json
import re

import httpx

from app.config import settings
from app.models import FirmProfile, ParsedDeck, TriageResult

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TRIAGE_MODEL = "nvidia/nemotron-3.5-lightning:free"

_TRIAGE_JSON_SCHEMA = {
    "name": "triage_result",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "flag": {"type": "string", "enum": ["relevant", "review", "not_relevant"]},
            "reason": {"type": "string"},
        },
        "required": ["flag", "reason"],
        "additionalProperties": False,
    },
}


class TriageAgent:
    """Cheap, fast pre-filter that runs on every inbound deck.

    This is NOT the deep-research agent (that's Track B) — one OpenRouter
    call, no web search, no multi-turn loop. It only judges fit against the
    firm's stated criteria and basic deck credibility.
    """

    async def classify(self, deck: ParsedDeck, firm: FirmProfile | None) -> TriageResult:
        prompt = self._build_prompt(deck, firm)
        model = settings.openrouter_model or TRIAGE_MODEL

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    # Best-effort — structured output support varies by
                    # provider/model on OpenRouter, so the prompt itself also
                    # asks for JSON and we parse defensively below.
                    "response_format": {"type": "json_schema", "json_schema": _TRIAGE_JSON_SCHEMA},
                },
            )
            response.raise_for_status()
            payload = response.json()

        content = payload["choices"][0]["message"]["content"]
        return TriageResult.model_validate(self._extract_json(content))

    @staticmethod
    def _extract_json(content: str) -> dict:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError(f"Triage model returned no JSON object: {content!r}")
        return json.loads(match.group(0))

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
            f"{deck.full_text}\n\n"
            'Respond with ONLY a JSON object of the exact shape '
            '{"flag": "relevant"|"review"|"not_relevant", "reason": "<one sentence>"} '
            "and nothing else."
        )
