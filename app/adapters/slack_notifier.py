from __future__ import annotations

import logging
import re

import httpx

from app.config import settings
from app.models import DiligenceReport, FounderSummary

logger = logging.getLogger(__name__)

CHAT_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
REQUEST_TIMEOUT_SECONDS = 15.0

# Every limit below was read off the live Block Kit reference (fetched while
# following the `slack:block-kit` skill), not from memory:
#   header-block.md   — `text` must be a plain_text object, max 150 chars.
#   section-block.md  — `text` min length 1, max length 3000 chars.
#   block-kit index   — "You can include up to 50 blocks in each message".
HEADER_TEXT_MAX_CHARS = 150
SECTION_TEXT_MAX_CHARS = 3000
MESSAGE_MAX_BLOCKS = 50
# The notification fallback is plain message text; keep it short enough to be
# useful in a push notification rather than dumping the whole one-liner.
FALLBACK_TEXT_MAX_CHARS = 300

ELLIPSIS = "…"


class SlackNotifierError(RuntimeError):
    """A Slack delivery failure.

    Carries Slack's own error string verbatim so a developer sees exactly what
    Slack objected to. Never carries the credential: an incoming-webhook URL
    *is* the secret, so it is never interpolated into a message or logged.
    """


# --------------------------------------------------------------------------
# text safety
# --------------------------------------------------------------------------

# formatting-message-text.md: "Slack uses `&`, `<`, and `>` as control
# characters for special parsing in text objects, so they must be converted to
# HTML entities if they're not going to be used for their parsing purpose."
# The docs draw no distinction between plain_text and mrkdwn objects here, so
# the same escaping is applied to both. `&` must be replaced first, otherwise
# the ampersands introduced by the later two would be double-escaped.
_TEXT_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))

# A truncation cut can land in the middle of an inserted `&amp;`, which would
# leave a dangling `&am` in the message; this strips such a fragment.
_PARTIAL_ENTITY = re.compile(r"&[#0-9A-Za-z]*$")


def _escape(value: object) -> str:
    text = str(value)
    for raw, entity in _TEXT_ESCAPES:
        text = text.replace(raw, entity)
    return text


def _truncate(text: str, limit: int) -> str:
    """Cut already-escaped text to `limit` characters without splitting an entity."""
    if len(text) <= limit:
        return text
    cut = _PARTIAL_ENTITY.sub("", text[: limit - 1]).rstrip()
    return (cut + ELLIPSIS) if cut else ELLIPSIS


def _escape_url(url: object) -> str:
    """Make a URL safe inside mrkdwn's `<url|label>` link form.

    `<`, `>` and `|` delimit the link form itself, so they are percent-encoded
    (keeping the target a valid URL) rather than turned into HTML entities.
    `&` is a Slack control character everywhere, so it is entity-escaped.
    """
    raw = str(url)
    for char, encoded in (("<", "%3C"), (">", "%3E"), ("|", "%7C")):
        raw = raw.replace(char, encoded)
    return raw.replace("&", "&amp;")


def _link(url: object, label: str) -> str:
    # formatting-message-text.md: a hyperlink with custom display text is
    # `<https://example.com|label>` — Slack mrkdwn, *not* markdown's
    # `[label](url)`, which renders literally.
    return f"<{_escape_url(url)}|{_escape(label)}>"


# --------------------------------------------------------------------------
# block builders
# --------------------------------------------------------------------------


def _header_block(name: str) -> dict:
    text = _escape(name).strip() or "Unnamed company"
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": _truncate(text, HEADER_TEXT_MAX_CHARS)},
    }


def _section_block(mrkdwn_text: str) -> dict:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": _truncate(mrkdwn_text, SECTION_TEXT_MAX_CHARS)},
    }


def _founder_line(founder: FounderSummary) -> str:
    """Render one founder: bolded name, bio, and a LinkedIn link only if there is one.

    A missing `linkedin_url` yields the bio as plain text with no hyperlink —
    never an empty `<|LinkedIn>` and never a literal "None".
    """
    name = _escape(founder.name).strip() or "Unnamed founder"
    bio = _escape(founder.bio_one_liner).strip()

    line = f"*{name}*"
    if bio:
        line += f" — {bio}"
    if founder.linkedin_url is not None:
        line += f" ({_link(founder.linkedin_url, 'LinkedIn')})"
    return line


def build_message(report: DiligenceReport) -> dict:
    """Build the full `chat.postMessage`/webhook payload for a report.

    Carries exactly the four fields Track C owes: company name, one-liner,
    per-founder bio (+ LinkedIn link when present), and the website link.
    Deliberately carries no score, weight or composite of any kind.
    """
    company = report.company
    blocks: list[dict] = [_header_block(company.name)]

    one_liner = _escape(company.one_liner).strip()
    if one_liner:
        blocks.append(_section_block(one_liner))

    # Omit the line entirely when there is no website — Track B returns None
    # rather than fabricating a URL, so this is the common case.
    if company.website_url is not None:
        website = str(company.website_url)
        blocks.append(_section_block(f"*Website:* {_link(website, website)}"))

    if report.founders:
        # Only emitted when there is at least one founder: a section block's
        # `text` has a documented minimum length of 1, so an empty founder
        # section would be rejected outright.
        blocks.append(_section_block("*Founders*"))

        budget = max(MESSAGE_MAX_BLOCKS - len(blocks), 0)
        shown = report.founders
        hidden = 0
        if len(shown) > budget:
            # Keep one block back for the "+N more" note.
            shown = report.founders[: max(budget - 1, 0)]
            hidden = len(report.founders) - len(shown)

        for founder in shown:
            blocks.append(_section_block(_founder_line(founder)))
        if hidden:
            blocks.append(_section_block(f"_+{hidden} more founder(s) not shown._"))

    # formatting-message-text.md: desktop notifications fall back to this field
    # and "Mobile notifications exclusively use message.text", so it must
    # summarise the blocks rather than be left empty.
    fallback = _escape(company.name).strip() or "Unnamed company"
    if one_liner:
        fallback += f" — {one_liner}"
    return {
        "text": _truncate(fallback, FALLBACK_TEXT_MAX_CHARS),
        "blocks": blocks[:MESSAGE_MAX_BLOCKS],
    }


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def _no_credential_error() -> SlackNotifierError:
    configured = []
    if (settings.slack_bot_token or "").strip():
        configured.append("SLACK_BOT_TOKEN is set but SLACK_CHANNEL_ID is missing")
    if (settings.slack_channel_id or "").strip():
        configured.append("SLACK_CHANNEL_ID is set but SLACK_BOT_TOKEN is missing")

    detail = f" ({'; '.join(configured)})" if configured else ""
    return SlackNotifierError(
        "No Slack credential configured, so the notification cannot be sent"
        f"{detail}. Set SLACK_WEBHOOK_URL (an incoming webhook — preferred, no "
        "scopes to configure), or set both SLACK_BOT_TOKEN and "
        "SLACK_CHANNEL_ID to post via chat.postMessage. See .env.example."
    )


def _raise_scrubbed(message: str) -> None:
    """Raise `SlackNotifierError` with no link back to the transport exception.

    `raise ... from None` only sets `__cause__ = None` and `__suppress_context__`.
    It does NOT clear `__context__`, so the original `httpx.RequestError` -- which
    carries `.request`, and therefore the `Authorization` header or the webhook
    URL -- stays reachable as `exc.__context__`. Default tracebacks honour
    `__suppress_context__`, but error-reporting tools that walk `__context__`
    directly for grouping do not, and would recover the credential.

    Raising from a helper means no exception is being handled at the point the
    error is constructed and raised, so `__context__` is never populated.
    """
    raise SlackNotifierError(message)


async def _post_via_webhook(client: httpx.AsyncClient, url: str, payload: dict) -> None:
    """POST the message JSON straight to an incoming webhook URL.

    sending-messages-using-incoming-webhooks.md: the message JSON *is* the whole
    body — there is no auth header and no `channel` field. Success is HTTP 200
    with the plain-text body `ok`; failure is a non-2xx status whose body is a
    bare error string such as `invalid_payload`, `invalid_token` or `no_service`.
    So here, unlike chat.postMessage, the HTTP status is the signal.
    """
    try:
        response = await client.post(url, json=payload)
    except httpx.RequestError as exc:
        # httpx attaches the failed request -- and therefore the webhook URL,
        # which IS the credential -- to the exception. Capture only the parts
        # that are safe to show, then raise outside this handler so no reference
        # to the original survives on the new exception. See _raise_scrubbed.
        detail = f"{type(exc).__name__}: {exc}"
    else:
        detail = ""

    if detail:
        _raise_scrubbed(
            f"Could not reach the Slack incoming webhook: {detail}. "
            "The webhook URL is the credential, so it is not echoed here."
        )

    if response.is_success:
        logger.info("Posted Slack notification via incoming webhook")
        return

    body = (response.text or "").strip()[:300]
    raise SlackNotifierError(
        f"Slack incoming webhook rejected the message: HTTP {response.status_code} "
        f"{body or '<empty response body>'}. The webhook URL is the credential, "
        "so it is not echoed here — check SLACK_WEBHOOK_URL."
    )


async def _post_via_bot_token(
    client: httpx.AsyncClient, token: str, channel: str, payload: dict
) -> None:
    """POST the message to chat.postMessage with a bot token.

    chat.postmessage.md: the token goes in an `Authorization: Bearer` header,
    `channel` is required alongside the message, and a JSON body needs
    `application/json; charset=utf-8`.
    """
    try:
        response = await client.post(
            CHAT_POST_MESSAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={**payload, "channel": channel},
        )
    except httpx.RequestError as exc:
        # Same hazard as the webhook path, and worse: the failed request carries
        # the `Authorization: Bearer <token>` header, so letting httpx's own
        # exception escape hands the bot token to any upstream handler that logs
        # `exc.request`. Scrub it the same way.
        detail = f"{type(exc).__name__}: {exc}"
    else:
        detail = ""

    if detail:
        _raise_scrubbed(
            f"Could not reach Slack chat.postMessage: {detail}. "
            "The bot token is the credential, so it is not echoed here."
        )

    # The critical asymmetry with the webhook path: chat.postMessage answers
    # HTTP 200 even when it fails, carrying {"ok": false, "error": "..."} in the
    # body (chat.postmessage.md, and "Always check `ok`" per slack:slack-api).
    # Checking only the status code would silently swallow every auth and schema
    # error, so the body is parsed and `ok` is checked on every response.
    if not response.is_success:
        raise SlackNotifierError(
            f"Slack chat.postMessage returned HTTP {response.status_code}: "
            f"{(response.text or '').strip()[:300] or '<empty response body>'}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise SlackNotifierError(
            "Slack chat.postMessage returned a non-JSON body: "
            f"{(response.text or '').strip()[:300] or '<empty response body>'}"
        ) from exc

    if not data.get("ok"):
        error = data.get("error") or "<no error field in response>"
        detail = f"Slack chat.postMessage failed: {error}"
        # missing_scope names the gap explicitly; surface it verbatim.
        if data.get("needed"):
            detail += f" (needed={data['needed']}, provided={data.get('provided')})"
        messages = (data.get("response_metadata") or {}).get("messages")
        if messages:
            detail += f" — {messages}"
        raise SlackNotifierError(detail)

    logger.info(
        "Posted Slack notification via chat.postMessage (channel=%s, ts=%s)",
        channel,
        data.get("ts"),
    )


async def send_slack_notification(report: DiligenceReport) -> None:
    """Post a finished report to Slack as a Block Kit message.

    Prefers an incoming webhook, falls back to a bot token, and raises
    `SlackNotifierError` when neither is configured or when Slack rejects the
    message. Nothing is swallowed: this is the last step before the demo's
    payoff, so breakage has to be visible now rather than live.
    """
    payload = build_message(report)

    webhook_url = (settings.slack_webhook_url or "").strip()
    bot_token = (settings.slack_bot_token or "").strip()
    channel_id = (settings.slack_channel_id or "").strip()

    if not webhook_url and not (bot_token and channel_id):
        raise _no_credential_error()

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        if webhook_url:
            await _post_via_webhook(client, webhook_url, payload)
        else:
            await _post_via_bot_token(client, bot_token, channel_id, payload)
