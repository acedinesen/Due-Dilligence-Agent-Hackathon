from __future__ import annotations

import asyncio

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from app.config import settings
from app.models import FirmProfile, ParsedDeck, TriageResult

# Fallback only — the real value comes from settings.anthropic_model (.env).
DEFAULT_MODEL = "claude-sonnet-5"

# TriageResult is two fields (a 3-value enum + a string), so grammar-constrained
# decoding needs a handful of output tokens. 1024 leaves room for a full-sentence
# reason without ever letting this cheap pre-filter turn into an essay.
TRIAGE_MAX_TOKENS = 1_024

# Two different timeouts, because they bound two different things:
#
#   * TRANSPORT_TIMEOUT_SECONDS is handed to the SDK/httpx. A bare float there
#     becomes httpx's *per-operation* timeout (connect/read/write each get it),
#     which is why the old OpenRouter code's `timeout=60.0` never bounded
#     anything: a server that dribbled bytes kept resetting the read clock, and
#     157-second calls completed "successfully".
#   * TOTAL_TIMEOUT_SECONDS is the wall-clock bound, enforced with
#     asyncio.timeout around the whole call. It is the only thing that actually
#     caps latency, because the SDK also retries internally — per-attempt
#     timeouts multiply, a total does not.
#
# Sizing: this call should take a few seconds (docs/track-a-drive-trigger.md
# "Sanity-check the triage call is fast"). 20s per attempt is generous enough to
# absorb a slow first token, and 30s total keeps a wedged triage comfortably
# inside the 45s poll interval so cycles can never pile up on each other.
TRANSPORT_TIMEOUT_SECONDS = 20.0
TOTAL_TIMEOUT_SECONDS = 30.0

# One retry, not the SDK default of two: a failed triage costs nothing to redo —
# the deck stays in Drive Inbox/ and is picked up on the next poll — so it is
# cheaper to give up quickly than to sit inside a retry backoff.
MAX_RETRIES = 1

# Deck text is truncated before it reaches the prompt. The old code interpolated
# `full_text` unbounded, so one 300-page PDF could blow past the context window
# (or just cost real money) on a call whose whole point is being cheap. 20k
# characters is ~5k tokens: comfortably a whole 20-30 slide deck, and triage
# signal (market, team, traction, ask) lives in the early slides regardless.
MAX_DECK_CHARS = 20_000
_TRUNCATION_MARKER = "\n\n[... deck text truncated for triage ...]"


class TriageError(RuntimeError):
    """Triage could not produce a TriageResult."""


class TriageAgent:
    """Cheap, fast pre-filter that runs on every inbound deck.

    This is NOT the deep-research agent (that's Track B). Per
    docs/track-a-drive-trigger.md step 3 it is exactly **one**
    `client.messages.parse(..., output_format=TriageResult)` call: no web
    search, no tools of any kind, no multi-turn loop. Giving this call research
    tools would defeat the entire point of a pre-filter that runs on the decks
    that get rejected too.

    Structured output comes from grammar-constrained decoding
    (`output_format=TriageResult`), which is why there is no JSON-scraping
    fallback here — the model cannot emit prose around the object. Unlike
    Track B's `DiligenceReport`, this schema is two fields, so the API's
    "compiled grammar is too large" limit is very unlikely to apply -- but that
    is NOT yet confirmed against the live API: every attempt so far was rejected
    at authentication before the request reached grammar compilation. Confirm it
    with one real call as soon as a valid key is available.
    """

    def __init__(
        self,
        client: AsyncAnthropic | None = None,
        model: str | None = None,
    ) -> None:
        # Built lazily so importing this module — and constructing the agent at
        # app/pipeline startup — never requires an API key.
        self._client = client
        self._model = model or settings.anthropic_model or DEFAULT_MODEL

    async def classify(self, deck: ParsedDeck, firm: FirmProfile | None) -> TriageResult:
        client = self._get_client()
        prompt = self._build_prompt(deck, firm)

        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                response = await client.messages.parse(
                    model=self._model,
                    max_tokens=TRIAGE_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                    output_format=TriageResult,
                )
        except TimeoutError as exc:
            raise TriageError(
                f"Triage of {deck.filename!r} exceeded {TOTAL_TIMEOUT_SECONDS}s. This "
                "call is meant to take a few seconds; treat a timeout as an API "
                "problem, not a slow deck. The deck stays in Drive Inbox/ and is "
                "retried on the next poll."
            ) from exc
        except ValidationError as exc:
            # Raised client-side by the SDK while validating the model's
            # grammar-constrained output against TriageResult.
            raise TriageError(
                f"Triage output for {deck.filename!r} did not validate against "
                f"TriageResult: {exc}"
            ) from exc

        result = response.parsed_output
        if result is None:
            # Typed Optional[T] by the SDK: happens on a refusal or a
            # max_tokens cut-off mid-object, where there is no object to parse.
            raise TriageError(
                f"Triage returned no parsed output for {deck.filename!r} "
                f"(stop_reason={getattr(response, 'stop_reason', None)!r}). No flag "
                "was produced, so the deck must not be filed or handed downstream."
            )

        return result

    # ---------------------------------------------------------------- client

    def _get_client(self) -> AsyncAnthropic:
        if self._client is not None:
            return self._client

        # Checked at call time, not import time, mirroring app/diligence.py:
        # importing the pipeline must not require a key.
        api_key = (settings.anthropic_api_key or "").strip()
        if not api_key:
            raise TriageError(
                "ANTHROPIC_API_KEY is not set, so the triage agent cannot run. Add "
                "ANTHROPIC_API_KEY=<your key> to .env (see .env.example) or set it as "
                "an environment variable, then retry. Nothing else is missing — the "
                "deck was parsed fine."
            )

        self._client = AsyncAnthropic(
            api_key=api_key,
            timeout=TRANSPORT_TIMEOUT_SECONDS,
            max_retries=MAX_RETRIES,
        )
        return self._client

    # ---------------------------------------------------------------- prompt

    @staticmethod
    def _deck_text(deck: ParsedDeck) -> str:
        if len(deck.full_text) <= MAX_DECK_CHARS:
            return deck.full_text
        return deck.full_text[:MAX_DECK_CHARS] + _TRUNCATION_MARKER

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
            "Give one sentence of reasoning, grounded only in the text below.\n\n"
            f"Deck filename: {deck.filename}\n\n"
            "Deck text:\n"
            f"{self._deck_text(deck)}"
        )
