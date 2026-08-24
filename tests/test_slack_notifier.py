"""Tests for the Slack Block Kit delivery adapter.

Async functions are driven with `asyncio.run` rather than a pytest async
plugin: the repo pins plain `pytest` and no `pytest-asyncio`/`anyio`, and this
adapter is small enough not to justify a new dependency.

HTTP is faked with `httpx.MockTransport`, so no request ever leaves the
machine and no Slack credential is needed to run these.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.adapters import slack_notifier
from app.adapters.slack_notifier import (
    MESSAGE_MAX_BLOCKS,
    SECTION_TEXT_MAX_CHARS,
    SlackNotifierError,
    build_message,
    send_slack_notification,
)
from app.config import settings
from app.models import CompanySummary, DiligenceReport, FounderSummary

FIXTURES = Path(__file__).parent / "fixtures"
NO_LINKS_FIXTURE = FIXTURES / "sample_report.json"
WITH_LINKS_FIXTURE = FIXTURES / "sample_report_urls_populated.json"

FAKE_WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/xxxxSECRETxxxx"
FAKE_BOT_TOKEN = "xoxb-000-000-fakeSECRETtoken"
FAKE_CHANNEL_ID = "C0123456789"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def load_report(path: Path) -> DiligenceReport:
    return DiligenceReport.model_validate_json(path.read_text())


def section_texts(payload: dict) -> list[str]:
    return [
        block["text"]["text"] for block in payload["blocks"] if block["type"] == "section"
    ]


def use_credentials(
    monkeypatch,
    *,
    webhook: str | None = None,
    token: str | None = None,
    channel: str | None = None,
) -> None:
    """Pin all three Slack settings so a real local .env cannot leak in."""
    monkeypatch.setattr(settings, "slack_webhook_url", webhook)
    monkeypatch.setattr(settings, "slack_bot_token", token)
    monkeypatch.setattr(settings, "slack_channel_id", channel)


def capture_requests(monkeypatch, handler):
    """Route every httpx.AsyncClient the adapter opens through MockTransport."""
    sent: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(slack_notifier.httpx, "AsyncClient", factory)
    return sent


def ok_webhook(_request: httpx.Request) -> httpx.Response:
    # A webhook answers 200 with the plain-text body "ok", not JSON.
    return httpx.Response(200, text="ok")


# --------------------------------------------------------------------------
# rendering: the four required fields
# --------------------------------------------------------------------------


def test_all_four_fields_render_when_populated():
    report = load_report(WITH_LINKS_FIXTURE)
    payload = build_message(report)

    header = payload["blocks"][0]
    assert header["type"] == "header"
    assert header["text"]["type"] == "plain_text"  # header-block.md: plain_text only
    assert header["text"]["text"] == report.company.name

    texts = section_texts(payload)
    assert report.company.one_liner in texts
    assert f"*Website:* <{report.company.website_url}|{report.company.website_url}>" in texts

    for founder in report.founders:
        line = next(t for t in texts if founder.name in t)
        assert founder.bio_one_liner in line
        # mrkdwn link form is <url|label>, not markdown's [label](url).
        assert f"<{founder.linkedin_url}|LinkedIn>" in line


def test_missing_urls_render_without_none_or_broken_links():
    """The real committed fixture: website_url and both linkedin_url are null."""
    report = load_report(NO_LINKS_FIXTURE)
    assert report.company.website_url is None
    assert all(f.linkedin_url is None for f in report.founders)

    payload = build_message(report)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "None" not in serialized
    assert "Website" not in serialized  # the whole line is omitted
    assert "LinkedIn" not in serialized
    assert "<|" not in serialized and "|>" not in serialized  # no empty link
    assert "]()" not in serialized

    # The bios still appear, as plain text.
    texts = section_texts(payload)
    for founder in report.founders:
        assert any(founder.bio_one_liner in t for t in texts)


def test_no_founders_emits_no_founder_section():
    report = load_report(NO_LINKS_FIXTURE).model_copy(update={"founders": []})
    payload = build_message(report)

    assert "*Founders*" not in section_texts(payload)
    # section-block.md: `text` has a documented minimum length of 1, so an
    # empty section would be rejected by Slack outright.
    assert all(block["text"]["text"] for block in payload["blocks"])


def test_fallback_text_is_present_and_plain():
    payload = build_message(load_report(WITH_LINKS_FIXTURE))
    # Mobile notifications use message.text exclusively, so it must be filled.
    assert payload["text"].startswith("Verdiq — Verdiq turns raw utility")
    assert "*" not in payload["text"] and "<" not in payload["text"]


# --------------------------------------------------------------------------
# rendering: defensive limits and escaping
# --------------------------------------------------------------------------


def test_long_values_are_truncated_to_slack_limits():
    report = load_report(NO_LINKS_FIXTURE).model_copy(
        update={
            "company": CompanySummary(
                name="N" * 400,
                one_liner="L" * 6000,
                website_url=None,
            )
        }
    )
    payload = build_message(report)

    assert len(payload["blocks"][0]["text"]["text"]) == 150  # header max
    assert len(payload["blocks"][1]["text"]["text"]) == SECTION_TEXT_MAX_CHARS
    assert payload["blocks"][1]["text"]["text"].endswith("…")


def test_many_founders_stay_within_the_block_limit():
    founders = [
        FounderSummary(name=f"Founder {i}", bio_one_liner=f"Bio {i}") for i in range(120)
    ]
    payload = build_message(
        load_report(NO_LINKS_FIXTURE).model_copy(update={"founders": founders})
    )

    assert len(payload["blocks"]) <= MESSAGE_MAX_BLOCKS
    assert "more founder(s) not shown" in section_texts(payload)[-1]


def test_control_characters_are_html_escaped():
    """`&`, `<` and `>` are Slack control characters in every text object."""
    report = load_report(NO_LINKS_FIXTURE).model_copy(
        update={
            "company": CompanySummary(name="A & B <Labs>", one_liner="x > y & z"),
            "founders": [FounderSummary(name="R&D Lead", bio_one_liner="<script>")],
        }
    )
    payload = build_message(report)
    serialized = json.dumps(payload)

    assert payload["blocks"][0]["text"]["text"] == "A &amp; B &lt;Labs&gt;"
    assert "x &gt; y &amp; z" in section_texts(payload)
    assert "&lt;script&gt;" in serialized
    assert "<script>" not in serialized


# --------------------------------------------------------------------------
# credential selection
# --------------------------------------------------------------------------


def test_missing_credentials_raise_an_actionable_error(monkeypatch):
    use_credentials(monkeypatch)
    with pytest.raises(SlackNotifierError) as excinfo:
        asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))

    message = str(excinfo.value)
    assert "SLACK_WEBHOOK_URL" in message
    assert "SLACK_BOT_TOKEN" in message
    assert "SLACK_CHANNEL_ID" in message


def test_bot_token_without_channel_is_not_treated_as_configured(monkeypatch):
    use_credentials(monkeypatch, token=FAKE_BOT_TOKEN)
    with pytest.raises(SlackNotifierError) as excinfo:
        asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))

    assert "SLACK_CHANNEL_ID is missing" in str(excinfo.value)
    assert FAKE_BOT_TOKEN not in str(excinfo.value)


def test_webhook_wins_over_a_bot_token(monkeypatch):
    use_credentials(
        monkeypatch,
        webhook=FAKE_WEBHOOK_URL,
        token=FAKE_BOT_TOKEN,
        channel=FAKE_CHANNEL_ID,
    )
    sent = capture_requests(monkeypatch, ok_webhook)
    asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))

    assert str(sent[0].url) == FAKE_WEBHOOK_URL


# --------------------------------------------------------------------------
# webhook transport
# --------------------------------------------------------------------------


def test_webhook_posts_bare_payload_with_no_auth_or_channel(monkeypatch):
    use_credentials(monkeypatch, webhook=FAKE_WEBHOOK_URL)
    sent = capture_requests(monkeypatch, ok_webhook)
    report = load_report(WITH_LINKS_FIXTURE)
    asyncio.run(send_slack_notification(report))

    assert len(sent) == 1
    request = sent[0]
    assert request.method == "POST"
    assert "authorization" not in request.headers
    assert request.headers["content-type"] == "application/json"

    body = json.loads(request.content)
    assert "channel" not in body
    assert body == build_message(report)


def test_webhook_failure_raises_with_slacks_error_and_hides_the_url(monkeypatch):
    use_credentials(monkeypatch, webhook=FAKE_WEBHOOK_URL)
    # A webhook signals failure with a non-2xx status and a bare error string.
    capture_requests(
        monkeypatch, lambda _r: httpx.Response(403, text="invalid_token")
    )
    with pytest.raises(SlackNotifierError) as excinfo:
        asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))

    message = str(excinfo.value)
    assert "invalid_token" in message
    assert "403" in message
    assert FAKE_WEBHOOK_URL not in message
    assert "hooks.slack.com" not in message


def test_webhook_transport_error_does_not_leak_the_url(monkeypatch):
    use_credentials(monkeypatch, webhook=FAKE_WEBHOOK_URL)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    capture_requests(monkeypatch, boom)
    with pytest.raises(SlackNotifierError) as excinfo:
        asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))

    assert "connection refused" in str(excinfo.value)
    # The chained cause would carry the request (and so the URL) with it.
    assert excinfo.value.__cause__ is None
    assert FAKE_WEBHOOK_URL not in repr(excinfo.value)


# --------------------------------------------------------------------------
# bot-token transport
# --------------------------------------------------------------------------


def test_bot_token_sends_channel_and_bearer_auth(monkeypatch):
    use_credentials(monkeypatch, token=FAKE_BOT_TOKEN, channel=FAKE_CHANNEL_ID)
    sent = capture_requests(
        monkeypatch,
        lambda _r: httpx.Response(200, json={"ok": True, "ts": "1700000000.000100"}),
    )
    asyncio.run(send_slack_notification(load_report(WITH_LINKS_FIXTURE)))

    request = sent[0]
    assert str(request.url) == "https://slack.com/api/chat.postMessage"
    assert request.headers["authorization"] == f"Bearer {FAKE_BOT_TOKEN}"
    assert request.headers["content-type"] == "application/json; charset=utf-8"

    body = json.loads(request.content)
    assert body["channel"] == FAKE_CHANNEL_ID
    assert body["blocks"] and body["text"]


def test_bot_token_ok_false_on_http_200_is_detected(monkeypatch):
    """chat.postMessage answers HTTP 200 even when it fails.

    Checking the status code alone would silently swallow every auth and
    schema error, so the body must be parsed and `ok` checked.
    """
    use_credentials(monkeypatch, token=FAKE_BOT_TOKEN, channel=FAKE_CHANNEL_ID)
    capture_requests(
        monkeypatch,
        lambda _r: httpx.Response(
            200,
            json={
                "ok": False,
                "error": "invalid_blocks",
                "response_metadata": {
                    "messages": ["[ERROR] missing required field: type [json-pointer:/blocks/0]"]
                },
            },
        ),
    )
    with pytest.raises(SlackNotifierError) as excinfo:
        asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))

    message = str(excinfo.value)
    assert "invalid_blocks" in message
    assert "missing required field: type" in message
    assert FAKE_BOT_TOKEN not in message


def test_bot_token_missing_scope_reports_needed_and_provided(monkeypatch):
    use_credentials(monkeypatch, token=FAKE_BOT_TOKEN, channel=FAKE_CHANNEL_ID)
    capture_requests(
        monkeypatch,
        lambda _r: httpx.Response(
            200,
            json={
                "ok": False,
                "error": "missing_scope",
                "needed": "chat:write",
                "provided": "channels:read",
            },
        ),
    )
    with pytest.raises(SlackNotifierError) as excinfo:
        asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))

    message = str(excinfo.value)
    assert "missing_scope" in message
    assert "needed=chat:write" in message
    assert "provided=channels:read" in message


def test_bot_token_non_json_body_raises(monkeypatch):
    use_credentials(monkeypatch, token=FAKE_BOT_TOKEN, channel=FAKE_CHANNEL_ID)
    capture_requests(monkeypatch, lambda _r: httpx.Response(200, text="<html>502</html>"))
    with pytest.raises(SlackNotifierError, match="non-JSON body"):
        asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))


# --------------------------------------------------------------------------
# credential containment on transport failure
#
# A network error is the realistic failure (dropped connection, DNS, timeout),
# and httpx attaches the failed Request to the exception it raises. That request
# carries the `Authorization: Bearer <token>` header on the bot-token path, and
# IS the credential on the webhook path. So a transport error must never surface
# the original httpx exception, on any attribute a log or error reporter reads.
# `raise ... from None` is NOT sufficient on its own: it clears `__cause__` but
# leaves `__context__` pointing at the original, so these assert `__context__`
# explicitly.
# --------------------------------------------------------------------------


def _refuse_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _leak_surfaces(exc: BaseException) -> dict[str, str]:
    """Every place a credential could realistically resurface."""
    import traceback

    context = getattr(exc, "__context__", None)
    return {
        "str": str(exc),
        "repr": repr(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        "cause": repr(getattr(exc, "__cause__", None)),
        "context": repr(context),
        "context_request": repr(getattr(context, "request", None)),
    }


@pytest.mark.parametrize(
    ("creds", "secret"),
    [
        ({"token": FAKE_BOT_TOKEN, "channel": FAKE_CHANNEL_ID}, FAKE_BOT_TOKEN),
        ({"webhook": FAKE_WEBHOOK_URL}, FAKE_WEBHOOK_URL),
    ],
    ids=["bot_token", "webhook"],
)
def test_transport_error_raises_scrubbed_and_leaks_nothing(monkeypatch, creds, secret):
    use_credentials(monkeypatch, **creds)
    capture_requests(monkeypatch, _refuse_connection)

    with pytest.raises(SlackNotifierError) as excinfo:
        asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))

    surfaces = _leak_surfaces(excinfo.value)
    leaked = sorted(name for name, text in surfaces.items() if secret in text)
    assert leaked == [], f"credential surfaced via: {leaked}"
    # Pin the mechanism, not just the outcome: a future refactor back to a plain
    # `raise ... from None` inside the except block would repopulate __context__.
    assert excinfo.value.__context__ is None
    assert excinfo.value.__cause__ is None


def test_transport_error_still_says_what_went_wrong(monkeypatch):
    # Scrubbing must not make the error useless to whoever is debugging it.
    use_credentials(monkeypatch, webhook=FAKE_WEBHOOK_URL)
    capture_requests(monkeypatch, _refuse_connection)

    with pytest.raises(SlackNotifierError) as excinfo:
        asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))

    message = str(excinfo.value)
    assert "ConnectError" in message
    assert "connection refused" in message


def test_channel_without_token_is_not_treated_as_configured(monkeypatch):
    # Mirror of test_bot_token_without_channel_is_not_treated_as_configured:
    # chat.postMessage needs both, so half the pair must not look configured.
    use_credentials(monkeypatch, token=None, channel=FAKE_CHANNEL_ID)

    def explode(_request):  # pragma: no cover - must never be reached
        raise AssertionError("no HTTP request should be attempted")

    capture_requests(monkeypatch, explode)

    with pytest.raises(SlackNotifierError) as excinfo:
        asyncio.run(send_slack_notification(load_report(NO_LINKS_FIXTURE)))
    assert "SLACK_WEBHOOK_URL" in str(excinfo.value)
