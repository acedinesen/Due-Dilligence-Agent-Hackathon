"""Unit tests for the cheap triage pre-filter (app/triage.py).

Everything here is offline: both providers' HTTP/SDK clients are stubs, so no
test ever spends a token or a real API call. The point of these tests is the
*contract* of each provider's call, not the model's judgement (which cannot be
asserted anyway):

* openrouter (default provider): one POST to OpenRouter's chat/completions
  endpoint with response_format=json_schema, reasoning explicitly disabled,
  and a bounded prompt.
* anthropic: one `messages.parse` with `output_format=TriageResult`, no
  tools, bounded prompt.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app import triage as triage_module
from app.models import FirmProfile, ParsedDeck, TriageResult
from app.triage import MAX_DECK_CHARS, TriageAgent, TriageError

# ------------------------------------------------------------------- shared


def _deck(full_text: str = "Seed-stage climate SaaS. 3 pilots. EUR 40k ARR.") -> ParsedDeck:
    return ParsedDeck(filename="deck.pdf", full_text=full_text)


FIRM = FirmProfile(
    id="generic_seed",
    name="Generic Pre-seed / Seed VC",
    criteria=["Large and credible market", "Early evidence of customer pull"],
)


# ================================================================ openrouter


class _FakeResponse:
    def __init__(self, json_body: dict, status_code: int = 200):
        self._json_body = json_body
        self.status_code = status_code
        self.text = json.dumps(json_body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", triage_module.OPENROUTER_URL)
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self) -> dict:
        return self._json_body


class _FakeHttpClient:
    """Records every POST; replays canned responses/errors in order."""

    def __init__(self, responses=None, errors=None, delay: float = 0.0):
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self._delay = delay
        self.calls: list[tuple[tuple, dict]] = []

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._errors:
            raise self._errors.pop(0)
        return self._responses.pop(0)


def _or_body(flag: str = "relevant", reason: str = "ok", finish_reason: str = "stop") -> dict:
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": json.dumps({"flag": flag, "reason": reason})},
            }
        ]
    }


def _only_call_kwargs(client: _FakeHttpClient) -> dict:
    assert len(client.calls) == 1, "must make exactly one HTTP call on the happy path"
    args, kwargs = client.calls[0]
    return kwargs


def test_openrouter_is_the_default_provider(monkeypatch):
    monkeypatch.setattr(triage_module.settings, "openrouter_api_key", "or-key")
    client = _FakeHttpClient(responses=[_FakeResponse(_or_body())])

    got = asyncio.run(TriageAgent(client=client).classify(_deck(), FIRM))

    assert got.flag == "relevant"
    assert len(client.calls) == 1


def test_openrouter_call_shape(monkeypatch):
    monkeypatch.setattr(triage_module.settings, "openrouter_api_key", "or-key")
    client = _FakeHttpClient(responses=[_FakeResponse(_or_body(flag="review"))])

    asyncio.run(
        TriageAgent(client=client, provider="openrouter", model="some/model:free").classify(
            _deck(), FIRM
        )
    )

    kwargs = _only_call_kwargs(client)
    payload = kwargs["json"]
    assert payload["model"] == "some/model:free"
    assert payload["reasoning"] == {"enabled": False}, "reasoning must be disabled"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    schema = payload["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"]) == {"flag", "reason"}
    assert len(payload["messages"]) == 1, "single-turn only — no multi-turn loop"
    assert set(payload["messages"][0]) == {"role", "content"}
    assert payload["messages"][0]["role"] == "user"
    assert kwargs["headers"]["Authorization"] == "Bearer or-key"


def test_openrouter_no_tools_are_ever_passed():
    """The whole value of this stage is being cheap — no web search here."""
    client = _FakeHttpClient(responses=[_FakeResponse(_or_body(flag="not_relevant"))])

    asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), FIRM))

    payload = client.calls[0][1]["json"]
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert "web_search" not in json.dumps(payload)


def test_openrouter_malformed_json_content_raises_triage_error():
    body = {"choices": [{"finish_reason": "length", "message": {"content": "{\n{\n  \"flag\": \"rel"}}]}
    client = _FakeHttpClient(responses=[_FakeResponse(body)])

    with pytest.raises(TriageError, match="not valid JSON"):
        asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), FIRM))


def test_openrouter_empty_content_raises_triage_error():
    body = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    client = _FakeHttpClient(responses=[_FakeResponse(body)])

    with pytest.raises(TriageError, match="empty content"):
        asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), FIRM))


def test_openrouter_out_of_enum_flag_raises_triage_error():
    body = _or_body(flag="maybe")
    client = _FakeHttpClient(responses=[_FakeResponse(body)])

    with pytest.raises(TriageError, match="did not validate against TriageResult"):
        asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), FIRM))


def test_openrouter_upstream_error_body_raises_triage_error():
    body = {"error": {"message": "rate limited", "code": 429}}
    client = _FakeHttpClient(responses=[_FakeResponse(body)])

    with pytest.raises(TriageError, match="OpenRouter returned an error"):
        asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), FIRM))


def test_openrouter_429_retries_once_then_succeeds(monkeypatch):
    monkeypatch.setattr(triage_module, "RETRY_BACKOFF_SECONDS", 0.0)
    error_response = _FakeResponse({}, status_code=429)
    client = _FakeHttpClient(responses=[error_response, _FakeResponse(_or_body())])

    got = asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), FIRM))

    assert got.flag == "relevant"
    assert len(client.calls) == 2, "one retry after the 429"


def test_openrouter_exhausts_retries_and_raises(monkeypatch):
    monkeypatch.setattr(triage_module, "RETRY_BACKOFF_SECONDS", 0.0)
    client = _FakeHttpClient(responses=[_FakeResponse({}, status_code=429), _FakeResponse({}, status_code=429)])

    with pytest.raises(TriageError, match="429"):
        asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), FIRM))

    assert len(client.calls) == triage_module.MAX_RETRIES + 1


def test_openrouter_total_timeout_is_enforced_as_wall_clock(monkeypatch):
    """A per-read HTTP timeout does not bound total latency; this does."""
    monkeypatch.setattr(triage_module, "TOTAL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(triage_module, "RETRY_BACKOFF_SECONDS", 0.0)
    client = _FakeHttpClient(responses=[_FakeResponse(_or_body())] * 3, delay=5.0)

    with pytest.raises(TriageError, match="exceeded"):
        asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), FIRM))


def test_openrouter_missing_api_key_raises_actionable_error_at_call_time(monkeypatch):
    monkeypatch.setattr(triage_module.settings, "openrouter_api_key", None)

    agent = TriageAgent(provider="openrouter")  # constructing it must not need a key

    with pytest.raises(TriageError, match="OPENROUTER_API_KEY is not set"):
        asyncio.run(agent.classify(_deck(), FIRM))


def test_openrouter_blank_api_key_is_treated_as_missing(monkeypatch):
    monkeypatch.setattr(triage_module.settings, "openrouter_api_key", "   ")

    with pytest.raises(TriageError, match="OPENROUTER_API_KEY is not set"):
        asyncio.run(TriageAgent(provider="openrouter").classify(_deck(), FIRM))


def test_openrouter_firm_criteria_appear_in_the_prompt():
    client = _FakeHttpClient(responses=[_FakeResponse(_or_body())])

    asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), FIRM))

    prompt = client.calls[0][1]["json"]["messages"][0]["content"]
    for criterion in FIRM.criteria:
        assert criterion in prompt


def test_openrouter_long_deck_text_is_truncated():
    long_text = "A" * (MAX_DECK_CHARS + 5_000)
    client = _FakeHttpClient(responses=[_FakeResponse(_or_body())])

    asyncio.run(TriageAgent(client=client, model="m").classify(_deck(long_text), FIRM))

    prompt = client.calls[0][1]["json"]["messages"][0]["content"]
    assert "A" * MAX_DECK_CHARS in prompt
    assert "A" * (MAX_DECK_CHARS + 1) not in prompt
    assert "truncated for triage" in prompt


# ================================================================= anthropic


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


def _fake_anthropic_client(result=None, error: Exception | None = None, delay: float = 0.0):
    messages = _FakeMessages(result=result, error=error, delay=delay)
    return SimpleNamespace(messages=messages), messages


def _response(parsed_output, stop_reason: str = "end_turn"):
    return SimpleNamespace(parsed_output=parsed_output, stop_reason=stop_reason)


def _only_anthropic_call_kwargs(messages: _FakeMessages) -> dict:
    assert len(messages.calls) == 1, "triage must make exactly one model call"
    args, kwargs = messages.calls[0]
    assert args == (), "the parse call must be keyword-only so kwargs assertions hold"
    return kwargs


def test_anthropic_valid_parse_returns_the_parsed_result():
    expected = TriageResult(flag="relevant", reason="Fits the fund's stage and sector.")
    client, messages = _fake_anthropic_client(result=_response(expected))

    got = asyncio.run(
        TriageAgent(client=client, provider="anthropic").classify(_deck(), FIRM)
    )

    assert got is expected


def test_anthropic_call_uses_messages_parse_with_triage_output_format():
    client, messages = _fake_anthropic_client(
        result=_response(TriageResult(flag="review", reason="Unclear traction."))
    )

    asyncio.run(
        TriageAgent(client=client, provider="anthropic", model="claude-sonnet-5").classify(
            _deck(), FIRM
        )
    )

    kwargs = _only_anthropic_call_kwargs(messages)
    assert kwargs["output_format"] is TriageResult
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["max_tokens"] == triage_module.TRIAGE_MAX_TOKENS
    assert len(kwargs["messages"]) == 1, "single-turn only — no multi-turn loop"
    assert set(kwargs["messages"][0]) == {"role", "content"}
    assert kwargs["messages"][0]["role"] == "user"


def test_anthropic_no_tools_are_ever_passed_to_the_api():
    client, messages = _fake_anthropic_client(
        result=_response(TriageResult(flag="not_relevant", reason="Not a deck."))
    )

    asyncio.run(TriageAgent(client=client, provider="anthropic").classify(_deck(), FIRM))

    kwargs = _only_anthropic_call_kwargs(messages)
    for forbidden in ("tools", "tool_choice", "betas", "extra_body"):
        assert forbidden not in kwargs, f"triage must not pass {forbidden!r}"
    assert "web_search" not in str(kwargs)


def test_anthropic_parsed_output_none_raises_triage_error():
    client, _ = _fake_anthropic_client(result=_response(None, stop_reason="max_tokens"))

    with pytest.raises(TriageError) as excinfo:
        asyncio.run(TriageAgent(client=client, provider="anthropic").classify(_deck(), FIRM))

    message = str(excinfo.value)
    assert "no parsed output" in message
    assert "deck.pdf" in message
    assert "max_tokens" in message, "the stop_reason is the diagnostic — keep it"


def test_anthropic_validation_error_from_the_sdk_raises_triage_error():
    try:
        TriageResult.model_validate({"flag": "maybe", "reason": "nope"})
    except ValidationError as exc:
        real_validation_error = exc
    else:  # pragma: no cover - guards the fixture, not the code under test
        pytest.fail("expected TriageResult to reject an out-of-enum flag")

    client, _ = _fake_anthropic_client(error=real_validation_error)

    with pytest.raises(TriageError) as excinfo:
        asyncio.run(TriageAgent(client=client, provider="anthropic").classify(_deck(), FIRM))

    assert "did not validate against TriageResult" in str(excinfo.value)


def test_anthropic_total_timeout_is_enforced_as_wall_clock(monkeypatch):
    """A per-read HTTP timeout does not bound total latency; this does."""
    monkeypatch.setattr(triage_module, "TOTAL_TIMEOUT_SECONDS", 0.01)
    client, _ = _fake_anthropic_client(
        result=_response(TriageResult(flag="relevant", reason="x")), delay=5.0
    )

    with pytest.raises(TriageError) as excinfo:
        asyncio.run(TriageAgent(client=client, provider="anthropic").classify(_deck(), FIRM))

    assert "exceeded" in str(excinfo.value)


def test_anthropic_missing_api_key_raises_actionable_error_at_call_time(monkeypatch):
    monkeypatch.setattr(triage_module.settings, "anthropic_api_key", None)

    agent = TriageAgent(provider="anthropic")  # constructing it must not need a key

    with pytest.raises(TriageError) as excinfo:
        asyncio.run(agent.classify(_deck(), FIRM))

    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY is not set" in message
    assert ".env" in message


def test_anthropic_blank_api_key_is_treated_as_missing(monkeypatch):
    monkeypatch.setattr(triage_module.settings, "anthropic_api_key", "   ")

    with pytest.raises(TriageError, match="ANTHROPIC_API_KEY is not set"):
        asyncio.run(TriageAgent(provider="anthropic").classify(_deck(), FIRM))


# -------------------------------------------------------------------- prompt
# (provider-agnostic — exercised once via the default/openrouter path)


def test_firm_none_still_works_and_omits_the_criteria_block():
    client = _FakeHttpClient(responses=[_FakeResponse(_or_body(flag="review"))])

    got = asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), None))

    assert got.flag == "review"
    prompt = client.calls[0][1]["json"]["messages"][0]["content"]
    assert "investment criteria" not in prompt


def test_firm_with_empty_criteria_omits_the_criteria_block():
    client = _FakeHttpClient(responses=[_FakeResponse(_or_body())])
    firm = FirmProfile(id="bare", name="Bare Fund", criteria=[])

    asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), firm))

    prompt = client.calls[0][1]["json"]["messages"][0]["content"]
    assert "investment criteria" not in prompt


def test_prompt_forbids_research_and_external_facts():
    client = _FakeHttpClient(responses=[_FakeResponse(_or_body())])

    asyncio.run(TriageAgent(client=client, model="m").classify(_deck(), FIRM))

    prompt = client.calls[0][1]["json"]["messages"][0]["content"]
    assert "Do NOT perform real research" in prompt
    assert "invent external" in prompt


def test_short_deck_text_is_passed_through_verbatim():
    text = "Tiny deck. Two founders. One customer."
    client = _FakeHttpClient(responses=[_FakeResponse(_or_body())])

    asyncio.run(TriageAgent(client=client, model="m").classify(_deck(text), FIRM))

    prompt = client.calls[0][1]["json"]["messages"][0]["content"]
    assert text in prompt
    assert "truncated for triage" not in prompt
