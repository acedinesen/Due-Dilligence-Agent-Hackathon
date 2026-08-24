"""Unit tests for the cheap triage pre-filter (app/triage.py).

Everything here is offline: the Anthropic client is a stub, so no test ever
spends a token. The point of these tests is the *contract* of the call — one
`messages.parse` with `output_format=TriageResult`, no tools, bounded prompt —
rather than the model's judgement, which cannot be asserted anyway.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import triage as triage_module
from app.models import FirmProfile, ParsedDeck, TriageResult
from app.triage import MAX_DECK_CHARS, TriageAgent, TriageError


class _FakeMessages:
    """Records the kwargs of the single `parse` call the agent is allowed."""

    def __init__(self, result=None, error: Exception | None = None, delay: float = 0.0):
        self._result = result
        self._error = error
        self._delay = delay
        self.calls: list[tuple[tuple, dict]] = []

    async def parse(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result


def _fake_client(result=None, error: Exception | None = None, delay: float = 0.0):
    messages = _FakeMessages(result=result, error=error, delay=delay)
    return SimpleNamespace(messages=messages), messages


def _response(parsed_output, stop_reason: str = "end_turn"):
    return SimpleNamespace(parsed_output=parsed_output, stop_reason=stop_reason)


def _deck(full_text: str = "Seed-stage climate SaaS. 3 pilots. EUR 40k ARR.") -> ParsedDeck:
    return ParsedDeck(filename="deck.pdf", full_text=full_text)


FIRM = FirmProfile(
    id="generic_seed",
    name="Generic Pre-seed / Seed VC",
    criteria=["Large and credible market", "Early evidence of customer pull"],
)


def _only_call_kwargs(messages: _FakeMessages) -> dict:
    assert len(messages.calls) == 1, "triage must make exactly one model call"
    args, kwargs = messages.calls[0]
    assert args == (), "the parse call must be keyword-only so kwargs assertions hold"
    return kwargs


# --------------------------------------------------------------- happy path


def test_valid_parse_returns_the_parsed_result():
    expected = TriageResult(flag="relevant", reason="Fits the fund's stage and sector.")
    client, messages = _fake_client(result=_response(expected))

    got = asyncio.run(TriageAgent(client=client).classify(_deck(), FIRM))

    assert got is expected
    assert got.flag == "relevant"
    assert got.reason == "Fits the fund's stage and sector."


def test_call_uses_messages_parse_with_triage_output_format():
    client, messages = _fake_client(
        result=_response(TriageResult(flag="review", reason="Unclear traction."))
    )

    asyncio.run(TriageAgent(client=client, model="claude-sonnet-5").classify(_deck(), FIRM))

    kwargs = _only_call_kwargs(messages)
    assert kwargs["output_format"] is TriageResult
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["max_tokens"] == triage_module.TRIAGE_MAX_TOKENS
    assert len(kwargs["messages"]) == 1, "single-turn only — no multi-turn loop"
    assert set(kwargs["messages"][0]) == {"role", "content"}
    assert kwargs["messages"][0]["role"] == "user"


# ------------------------------------------------------------- no web search


def test_no_tools_are_ever_passed_to_the_api():
    """The whole value of this stage is being cheap.

    docs/track-a-drive-trigger.md forbids web search here (that is Track B's
    job), so this asserts the *absence* of any tool wiring rather than trusting
    a comment — a future edit cannot quietly smuggle research into the
    pre-filter without failing this test.
    """
    client, messages = _fake_client(
        result=_response(TriageResult(flag="not_relevant", reason="Not a deck."))
    )

    asyncio.run(TriageAgent(client=client).classify(_deck(), FIRM))

    kwargs = _only_call_kwargs(messages)
    for forbidden in ("tools", "tool_choice", "betas", "extra_body"):
        assert forbidden not in kwargs, f"triage must not pass {forbidden!r}"
    assert "web_search" not in str(kwargs)


# ------------------------------------------------------------------ failures


def test_parsed_output_none_raises_triage_error():
    client, _ = _fake_client(result=_response(None, stop_reason="max_tokens"))

    with pytest.raises(TriageError) as excinfo:
        asyncio.run(TriageAgent(client=client).classify(_deck(), FIRM))

    message = str(excinfo.value)
    assert "no parsed output" in message
    assert "deck.pdf" in message
    assert "max_tokens" in message, "the stop_reason is the diagnostic — keep it"


def test_validation_error_from_the_sdk_raises_triage_error():
    try:
        TriageResult.model_validate({"flag": "maybe", "reason": "nope"})
    except ValidationError as exc:
        real_validation_error = exc
    else:  # pragma: no cover - guards the fixture, not the code under test
        pytest.fail("expected TriageResult to reject an out-of-enum flag")

    client, _ = _fake_client(error=real_validation_error)

    with pytest.raises(TriageError) as excinfo:
        asyncio.run(TriageAgent(client=client).classify(_deck(), FIRM))

    assert "did not validate against TriageResult" in str(excinfo.value)


def test_total_timeout_is_enforced_as_wall_clock(monkeypatch):
    """A per-read HTTP timeout does not bound total latency; this does."""
    monkeypatch.setattr(triage_module, "TOTAL_TIMEOUT_SECONDS", 0.01)
    client, _ = _fake_client(
        result=_response(TriageResult(flag="relevant", reason="x")), delay=5.0
    )

    with pytest.raises(TriageError) as excinfo:
        asyncio.run(TriageAgent(client=client).classify(_deck(), FIRM))

    assert "exceeded" in str(excinfo.value)


def test_missing_api_key_raises_actionable_error_at_call_time(monkeypatch):
    monkeypatch.setattr(triage_module.settings, "anthropic_api_key", None)

    agent = TriageAgent()  # constructing it must not need a key

    with pytest.raises(TriageError) as excinfo:
        asyncio.run(agent.classify(_deck(), FIRM))

    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY is not set" in message
    assert ".env" in message


def test_blank_api_key_is_treated_as_missing(monkeypatch):
    monkeypatch.setattr(triage_module.settings, "anthropic_api_key", "   ")

    with pytest.raises(TriageError, match="ANTHROPIC_API_KEY is not set"):
        asyncio.run(TriageAgent().classify(_deck(), FIRM))


# -------------------------------------------------------------------- prompt


def test_firm_criteria_appear_in_the_prompt():
    client, messages = _fake_client(
        result=_response(TriageResult(flag="relevant", reason="ok"))
    )

    asyncio.run(TriageAgent(client=client).classify(_deck(), FIRM))

    prompt = _only_call_kwargs(messages)["messages"][0]["content"]
    for criterion in FIRM.criteria:
        assert criterion in prompt


def test_firm_none_still_works_and_omits_the_criteria_block():
    client, messages = _fake_client(
        result=_response(TriageResult(flag="review", reason="No firm criteria given."))
    )

    got = asyncio.run(TriageAgent(client=client).classify(_deck(), None))

    assert got.flag == "review"
    prompt = _only_call_kwargs(messages)["messages"][0]["content"]
    assert "investment criteria" not in prompt


def test_firm_with_empty_criteria_omits_the_criteria_block():
    client, messages = _fake_client(
        result=_response(TriageResult(flag="review", reason="ok"))
    )
    firm = FirmProfile(id="bare", name="Bare Fund", criteria=[])

    asyncio.run(TriageAgent(client=client).classify(_deck(), firm))

    prompt = _only_call_kwargs(messages)["messages"][0]["content"]
    assert "investment criteria" not in prompt


def test_prompt_forbids_research_and_external_facts():
    client, messages = _fake_client(
        result=_response(TriageResult(flag="relevant", reason="ok"))
    )

    asyncio.run(TriageAgent(client=client).classify(_deck(), FIRM))

    prompt = _only_call_kwargs(messages)["messages"][0]["content"]
    assert "Do NOT perform real research" in prompt
    assert "invent external" in prompt


def test_long_deck_text_is_truncated():
    long_text = "A" * (MAX_DECK_CHARS + 5_000)
    client, messages = _fake_client(
        result=_response(TriageResult(flag="review", reason="ok"))
    )

    asyncio.run(TriageAgent(client=client).classify(_deck(long_text), FIRM))

    prompt = _only_call_kwargs(messages)["messages"][0]["content"]
    assert "A" * MAX_DECK_CHARS in prompt
    assert "A" * (MAX_DECK_CHARS + 1) not in prompt
    assert "truncated for triage" in prompt
    assert len(prompt) < len(long_text)


def test_short_deck_text_is_passed_through_verbatim():
    text = "Tiny deck. Two founders. One customer."
    client, messages = _fake_client(
        result=_response(TriageResult(flag="review", reason="ok"))
    )

    asyncio.run(TriageAgent(client=client).classify(_deck(text), FIRM))

    prompt = _only_call_kwargs(messages)["messages"][0]["content"]
    assert text in prompt
    assert "truncated for triage" not in prompt
