"""Tests for the Attio CRM delivery adapter (docs/track-c-delivery.md).

Track C's verification checklist is live-only — a real record in the real
workspace — which leaves the module's trickiest parts untested: the value
coercion that feeds Attio's *unique* `domains` attribute, the `personal-name`
split, byte-wise note truncation, and the retry policy that must not replay a
non-idempotent create. Those are exactly the places where a regression is
silent rather than loud, so they are pinned here.

Nothing in this file touches the network or needs a credential:

* the pure helpers are called directly,
* HTTP is faked with `httpx.MockTransport`,
* an autouse fixture makes any *real* transport raise, so an accidentally
  unmocked client fails the test instead of reaching api.attio.com,
* `save_to_attio` — the only function that reads the API key and opens a real
  client — is never called.

Async functions are driven with `asyncio.run`, matching `test_slack_notifier.py`:
the repo pins plain `pytest`, with no `pytest-asyncio`/`anyio` available.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.adapters import attio_client
from app.adapters.attio_client import (
    MAX_ATTEMPTS,
    NOTE_CONTENT_MAX_BYTES,
    NOTE_TRUNCATION_MARKER,
    RETRY_AFTER_CAP_SECONDS,
    RETRY_BACKOFF_SECONDS,
    AttioClient,
    AttioError,
    _build_note_markdown,
    _normalize_domain,
    _retry_after_seconds,
    _split_person_name,
    _truncate_note,
)
from app.models import DiligenceReport

FIXTURES = Path(__file__).parent / "fixtures"
# A real agent run with `website_url: null` and both founders' `linkedin_url:
# null` — the exact shape production sends when research finds no URL.
NO_LINKS_FIXTURE = FIXTURES / "sample_report.json"
WITH_LINKS_FIXTURE = FIXTURES / "sample_report_urls_populated.json"

COMPANY_ID = "11111111-2222-3333-4444-555555555555"


def _load(path: Path) -> DiligenceReport:
    return DiligenceReport.model_validate(json.loads(path.read_text()))


@pytest.fixture
def report_without_links() -> DiligenceReport:
    return _load(NO_LINKS_FIXTURE)


@pytest.fixture
def report_with_links() -> DiligenceReport:
    return _load(WITH_LINKS_FIXTURE)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn any unmocked HTTP into a test failure rather than a live call.

    The workspace under test is the team's real CRM, so "the test accidentally
    wrote a record" must be impossible, not merely unlikely.
    """

    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test attempted a real HTTP request")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


# --------------------------------------------------------------------------
# _normalize_domain — feeds the *unique* `domains` attribute, so a malformed
# value does not just look wrong, it can collide two companies onto one record.
# --------------------------------------------------------------------------


class TestNormalizeDomain:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://www.example.com/path/to/x?q=1", "example.com"),
            ("HTTPS://WWW.Example.COM", "example.com"),
            ("https://example.com:8443/x", "example.com"),
            ("https://user:pass@example.com/x", "example.com"),
            ("example.com", "example.com"),
            ("www.example.com", "example.com"),
            ("//example.com/x", "example.com"),
            # Only the leading label is dropped, and only when it really is
            # "www." — a bare host must survive untouched.
            ("wwwexample.com", "wwwexample.com"),
            ("sub.example.co.uk", "sub.example.co.uk"),
        ],
    )
    def test_reduces_a_url_to_its_bare_host(self, raw: str, expected: str) -> None:
        assert _normalize_domain(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "https://"])
    def test_missing_or_empty_input_yields_none(self, raw: object) -> None:
        # None must mean "omit the attribute", never the string "None".
        assert _normalize_domain(raw) is None

    def test_punycode_host_passes_through_unchanged(self) -> None:
        assert _normalize_domain("https://xn--tdaa.example/x") == "xn--tdaa.example"

    def test_unicode_idn_host_is_lowercased_but_not_punycoded(self) -> None:
        # Pinning current behaviour: no IDNA encoding happens, so a unicode
        # host reaches Attio as unicode. Fine for our inputs (research returns
        # ASCII hosts) but worth knowing before someone adds IDNA on top.
        assert _normalize_domain("https://SØREN.Example/x") == "søren.example"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://[::1]:8080/path", "::1"),
            ("http://[2001:db8::1]/", "2001:db8::1"),
        ],
    )
    def test_ipv6_literal_is_not_mangled(self, raw: str, expected: str) -> None:
        # Regression guard for a real bug: splitting the netloc on ":" by hand
        # returned "[", which would have been written into the *unique*
        # `domains` attribute. urlparse().hostname strips the brackets instead.
        assert _normalize_domain(raw) == expected


# --------------------------------------------------------------------------
# _split_person_name — Attio needs all three keys together, and on *write* a
# bare string means "Last, First", so a wrong split misfiles a person silently.
# --------------------------------------------------------------------------


class TestSplitPersonName:
    def test_two_tokens(self) -> None:
        assert _split_person_name("Jane Doe") == {
            "first_name": "Jane",
            "last_name": "Doe",
            "full_name": "Jane Doe",
        }

    def test_three_or_more_tokens_keep_everything_after_the_first(self) -> None:
        assert _split_person_name("Ada Byron King Lovelace") == {
            "first_name": "Ada",
            "last_name": "Byron King Lovelace",
            "full_name": "Ada Byron King Lovelace",
        }

    def test_mononym_gets_an_empty_last_name(self) -> None:
        # The key must still be present — Attio rejects a partial
        # `personal-name` object.
        assert _split_person_name("Cher") == {
            "first_name": "Cher",
            "last_name": "",
            "full_name": "Cher",
        }

    @pytest.mark.parametrize("raw", ["  Jane   Doe  ", "Jane\tDoe", "\nJane  Doe\n"])
    def test_surrounding_and_internal_whitespace_is_collapsed(self, raw: str) -> None:
        assert _split_person_name(raw) == {
            "first_name": "Jane",
            "last_name": "Doe",
            "full_name": "Jane Doe",
        }

    def test_nordic_characters_survive_intact(self) -> None:
        # The deployment is Copenhagen-facing, so mangled diacritics would be
        # the common case, not the exotic one.
        assert _split_person_name("Søren Bøgh-Ålund") == {
            "first_name": "Søren",
            "last_name": "Bøgh-Ålund",
            "full_name": "Søren Bøgh-Ålund",
        }

    def test_empty_name_produces_empty_strings_rather_than_raising(self) -> None:
        # Documents the current contract: the helper never raises, so an empty
        # founder name is Attio's problem to reject, not a local crash.
        assert _split_person_name("   ") == {
            "first_name": "",
            "last_name": "",
            "full_name": "",
        }


# --------------------------------------------------------------------------
# _truncate_note — POST /v2/notes answers 413 on an oversized body, and the
# limit is measured in encoded bytes, not characters.
# --------------------------------------------------------------------------

MARKER_BYTES = len(NOTE_TRUNCATION_MARKER.encode("utf-8"))


class TestTruncateNote:
    @pytest.mark.parametrize("size", [0, 1, 1_000, NOTE_CONTENT_MAX_BYTES])
    def test_content_within_the_cap_passes_through_byte_identical(self, size: int) -> None:
        content = "a" * size
        assert _truncate_note(content) == content

    def test_oversized_content_is_cut_and_marked(self) -> None:
        content = "a" * (NOTE_CONTENT_MAX_BYTES + 5_000)
        result = _truncate_note(content)
        assert result.endswith(NOTE_TRUNCATION_MARKER)
        assert len(result.encode("utf-8")) <= NOTE_CONTENT_MAX_BYTES
        # The marker is paid for out of the budget, not added on top of it.
        assert result[:-len(NOTE_TRUNCATION_MARKER)] == "a" * (NOTE_CONTENT_MAX_BYTES - MARKER_BYTES)

    def test_the_cap_is_bytes_not_characters(self) -> None:
        # 25k three-byte characters is far under the cap counted in characters
        # and far over it counted in bytes. Getting this wrong means a 413.
        content = "あ" * 25_000
        assert len(content) < NOTE_CONTENT_MAX_BYTES
        assert len(content.encode("utf-8")) > NOTE_CONTENT_MAX_BYTES
        assert _truncate_note(content) != content

    @pytest.mark.parametrize("pad", [0, 1, 2])
    def test_cutting_mid_character_never_emits_a_replacement_character(self, pad: int) -> None:
        # The budget is a byte count, so with multi-byte content the cut lands
        # inside a character. Shifting the alignment by 0/1/2 ASCII bytes
        # guarantees at least one of these cases is a true mid-character cut,
        # whatever the constants are set to. The partial bytes must be dropped,
        # not decoded into U+FFFD and shipped to the CRM.
        content = "x" * pad + "あ" * 25_000
        result = _truncate_note(content)

        assert result.endswith(NOTE_TRUNCATION_MARKER)
        kept = result[: -len(NOTE_TRUNCATION_MARKER)]
        assert "�" not in kept
        assert set(kept) <= {"x", "あ"}
        assert len(result.encode("utf-8")) <= NOTE_CONTENT_MAX_BYTES
        # Round-tripping the payload is what the HTTP layer will do.
        assert result.encode("utf-8").decode("utf-8") == result


# --------------------------------------------------------------------------
# _retry_after_seconds — Attio sends an HTTP-date, not a seconds integer.
# --------------------------------------------------------------------------


def _response_with_retry_after(value: str | None) -> httpx.Response:
    headers = {} if value is None else {"Retry-After": value}
    return httpx.Response(429, headers=headers, json={"message": "rate limited"})


class TestRetryAfterSeconds:
    def test_future_http_date_becomes_a_positive_delay(self) -> None:
        when = datetime.now(timezone.utc) + timedelta(seconds=5)
        delay = _retry_after_seconds(_response_with_retry_after(format_datetime(when, usegmt=True)))
        # HTTP-dates have one-second granularity, so the exact value drifts.
        assert delay is not None
        assert 0.0 < delay <= 5.0

    @pytest.mark.parametrize(
        "raw",
        [
            "Wed, 21 Oct 2015 07:28:00 GMT",
            "Wed, 21 Oct 2015 07:28:00",  # no timezone — read as UTC, not local
        ],
    )
    def test_past_http_date_clamps_to_zero(self, raw: str) -> None:
        # Clock skew between us and Attio must never produce a negative sleep,
        # which asyncio.sleep would accept and turn into a hot retry loop.
        assert _retry_after_seconds(_response_with_retry_after(raw)) == 0.0

    def test_future_http_date_beyond_the_cap_is_capped(self) -> None:
        when = datetime.now(timezone.utc) + timedelta(seconds=RETRY_AFTER_CAP_SECONDS * 10)
        delay = _retry_after_seconds(_response_with_retry_after(format_datetime(when, usegmt=True)))
        assert delay == RETRY_AFTER_CAP_SECONDS

    def test_plain_seconds_from_an_intermediary_is_tolerated(self) -> None:
        assert _retry_after_seconds(_response_with_retry_after("5")) == 5.0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (str(RETRY_AFTER_CAP_SECONDS * 20), RETRY_AFTER_CAP_SECONDS),
            ("-5", 0.0),
        ],
    )
    def test_plain_seconds_is_clamped_to_the_documented_range(
        self, raw: str, expected: float
    ) -> None:
        assert _retry_after_seconds(_response_with_retry_after(raw)) == expected

    @pytest.mark.parametrize("raw", [None, "", "not-a-date", "soon"])
    def test_absent_or_malformed_header_yields_none(self, raw: str | None) -> None:
        # None is the signal to fall back to exponential backoff, so it must
        # not be confused with 0.
        assert _retry_after_seconds(_response_with_retry_after(raw)) is None


# --------------------------------------------------------------------------
# mocked transport
# --------------------------------------------------------------------------


class Recorder:
    """A fake Attio that records requests and replays canned 200s."""

    def __init__(self, query_results: list[dict] | None = None) -> None:
        self.query_results = query_results or []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"data": self.query_results})
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": {"record_id": COMPANY_ID, "note_id": "note_1"},
                    "web_url": f"https://app.attio.com/x/{COMPANY_ID}",
                }
            },
        )

    def payloads(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.content]

    def last_payload(self) -> dict:
        return json.loads(self.requests[-1].content)


def _run(coro_factory, recorder: Recorder):
    """Drive one adapter coroutine against a mocked transport."""

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler)) as client:
            return await coro_factory(AttioClient(), client)

    return asyncio.run(go())


# --------------------------------------------------------------------------
# the None-omission contract, at the payload level
# (docs/track-c-delivery.md: "Don't print a raw `None` … omit that field")
# --------------------------------------------------------------------------


class TestPayloadOmitsMissingUrls:
    def test_missing_website_omits_the_domains_key_entirely(
        self, report_without_links: DiligenceReport
    ) -> None:
        recorder = Recorder()
        _run(lambda a, c: a.save_company(c, report_without_links), recorder)

        values = recorder.last_payload()["data"]["values"]
        assert "domains" not in values  # not [], not null
        assert set(values) == {"name", "description"}
        # `team` is deliberately never sent: an upsert replaces multiselect
        # values wholesale and would wipe the workspace's own team list.
        assert "team" not in values

    def test_missing_website_falls_back_to_query_then_create(
        self, report_without_links: DiligenceReport
    ) -> None:
        # Without a domain there is no unique attribute to upsert on, so the
        # adapter must not send a PUT (which Attio would reject).
        recorder = Recorder()
        _run(lambda a, c: a.save_company(c, report_without_links), recorder)

        assert [r.method for r in recorder.requests] == ["POST", "POST"]
        assert recorder.requests[0].url.path.endswith("/objects/companies/records/query")
        assert recorder.requests[1].url.path.endswith("/objects/companies/records")

    def test_present_website_is_upserted_on_the_normalized_domain(
        self, report_with_links: DiligenceReport
    ) -> None:
        recorder = Recorder()
        _run(lambda a, c: a.save_company(c, report_with_links), recorder)

        assert len(recorder.requests) == 1
        request = recorder.requests[0]
        assert request.method == "PUT"
        assert request.url.params["matching_attribute"] == "domains"
        # https://www.verdiq.io/product → the bare host, path and www dropped.
        assert recorder.last_payload()["data"]["values"]["domains"] == ["verdiq.io"]

    def test_missing_linkedin_omits_the_linkedin_key_entirely(
        self, report_without_links: DiligenceReport
    ) -> None:
        founder = report_without_links.founders[0]
        assert founder.linkedin_url is None

        recorder = Recorder()
        _run(lambda a, c: a.save_person(c, founder, COMPANY_ID), recorder)

        values = recorder.last_payload()["data"]["values"]
        assert "linkedin" not in values  # not [], not null, not [{"value": ""}]
        assert set(values) == {"name", "description", "company"}

    def test_present_linkedin_is_sent_as_text(
        self, report_with_links: DiligenceReport
    ) -> None:
        founder = report_with_links.founders[0]
        recorder = Recorder()
        _run(lambda a, c: a.save_person(c, founder, COMPANY_ID), recorder)

        values = recorder.last_payload()["data"]["values"]
        assert values["linkedin"] == [{"value": str(founder.linkedin_url)}]

    def test_the_literal_string_none_never_reaches_the_wire(
        self, report_without_links: DiligenceReport
    ) -> None:
        # The failure this guards is stringifying an absent URL: `str(None)`
        # writes a plausible-looking "None" into the CRM instead of leaving the
        # field empty. The fixture has three of them (website + two founders).
        recorder = Recorder()

        _run(lambda a, c: a.save_company(c, report_without_links), recorder)
        for founder in report_without_links.founders:
            _run(lambda a, c, f=founder: a.save_person(c, f, COMPANY_ID), recorder)

        payloads = recorder.payloads()
        assert len(payloads) == 6  # query + write, for the company and two founders
        for payload in payloads:
            assert "None" not in json.dumps(payload, ensure_ascii=False)

    def test_person_name_is_sent_as_the_object_form_not_a_bare_string(
        self, report_without_links: DiligenceReport
    ) -> None:
        # A bare string on *write* is parsed as "Last, First", so the object
        # form is the difference between the right and the wrong name.
        recorder = Recorder()
        founder = report_without_links.founders[0]
        _run(lambda a, c: a.save_person(c, founder, COMPANY_ID), recorder)

        name = recorder.last_payload()["data"]["values"]["name"]
        assert name == [_split_person_name(founder.name)]
        assert set(name[0]) == {"first_name", "last_name", "full_name"}


# --------------------------------------------------------------------------
# _build_note_markdown
# --------------------------------------------------------------------------

# Word-bounded so ordinary prose ("operating", "underscore") cannot trip it.
SCORE_FAMILY = re.compile(
    r"\b(scor(e|es|ed|ing)|weight(s|ed|ing)?|composite(s)?|rating(s)?|rated|rank(s|ed|ing)?)\b",
    re.IGNORECASE,
)


class TestNoteMarkdown:
    @pytest.mark.parametrize("fixture", [NO_LINKS_FIXTURE, WITH_LINKS_FIXTURE])
    def test_rendered_body_carries_no_score_of_any_kind(self, fixture: Path) -> None:
        # The team decided against composite/weighted scoring; the output layer
        # is where it is most tempting to reintroduce one, and checking the
        # *rendered* body catches a score that arrives via report content too.
        body = _build_note_markdown(_load(fixture))
        assert SCORE_FAMILY.search(body) is None

    def test_missing_website_produces_no_website_line(
        self, report_without_links: DiligenceReport
    ) -> None:
        assert "Website:" not in _build_note_markdown(report_without_links)

    def test_missing_linkedin_produces_no_linkedin_link(
        self, report_without_links: DiligenceReport
    ) -> None:
        body = _build_note_markdown(report_without_links)
        assert "[LinkedIn](" not in body
        # The founder is still listed, just without a hyperlink.
        for founder in report_without_links.founders:
            assert founder.name in body

    def test_populated_report_renders_both_links(
        self, report_with_links: DiligenceReport
    ) -> None:
        body = _build_note_markdown(report_with_links)
        assert f"Website: {report_with_links.company.website_url}" in body
        for founder in report_with_links.founders:
            assert f"([LinkedIn]({founder.linkedin_url}))" in body

    def test_body_starts_with_the_company_name_as_the_only_h1(
        self, report_with_links: DiligenceReport
    ) -> None:
        body = _build_note_markdown(report_with_links)
        assert body.startswith(f"# {report_with_links.company.name}\n")
        assert len([line for line in body.splitlines() if line.startswith("# ")]) == 1


# --------------------------------------------------------------------------
# retry policy — behaviour, then the source-level contract
# --------------------------------------------------------------------------


@pytest.fixture
def captured_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record retry sleeps instead of really sleeping.

    The module's only use of `asyncio` is `asyncio.sleep` (one call site), so
    swapping the module global keeps the patch inside this adapter rather than
    mutating the stdlib for everything else in the session.
    """
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(attio_client, "asyncio", SimpleNamespace(sleep=fake_sleep))
    return delays


class FlakyAttio:
    """Replays a scripted sequence of status codes, then 200s forever."""

    def __init__(self, statuses: list[int], retry_after: str | None = None) -> None:
        self.statuses = list(statuses)
        self.retry_after = retry_after
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 200:
            return httpx.Response(200, json={"data": {"id": {"record_id": COMPANY_ID}}})
        headers = {"Retry-After": self.retry_after} if self.retry_after else {}
        return httpx.Response(status, headers=headers, json={"message": "upstream said no"})


def _post(fake: FlakyAttio, **kwargs):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
            return await AttioClient()._request(
                client, "POST", "/objects/people/records", json={"data": {}}, **kwargs
            )

    return asyncio.run(go())


class TestRetryPolicy:
    def test_429_is_retried_and_then_succeeds(self, captured_delays: list[float]) -> None:
        fake = FlakyAttio([429])
        assert _post(fake) == {"id": {"record_id": COMPANY_ID}}
        assert fake.calls == 2
        assert captured_delays == [RETRY_BACKOFF_SECONDS]

    def test_429_is_retried_even_for_a_non_idempotent_create(
        self, captured_delays: list[float]
    ) -> None:
        # Attio documents that a 429'd request was not processed, so replaying
        # it cannot duplicate a record — the flag must not suppress this.
        fake = FlakyAttio([429, 429])
        assert _post(fake, retry_server_faults=False)
        assert fake.calls == 3

    def test_retry_after_header_overrides_the_backoff(self, captured_delays: list[float]) -> None:
        when = datetime.now(timezone.utc) + timedelta(seconds=4)
        fake = FlakyAttio([429], retry_after=format_datetime(when, usegmt=True))
        _post(fake)
        assert len(captured_delays) == 1
        assert 0.0 < captured_delays[0] <= 4.0

    def test_5xx_is_retried_for_an_idempotent_request(self, captured_delays: list[float]) -> None:
        fake = FlakyAttio([500, 503])
        assert _post(fake)
        assert fake.calls == 3
        # Exponential, since a 5xx carries no Retry-After here.
        assert captured_delays == [RETRY_BACKOFF_SECONDS, RETRY_BACKOFF_SECONDS * 2]

    def test_5xx_gives_up_after_max_attempts(self, captured_delays: list[float]) -> None:
        fake = FlakyAttio([500] * (MAX_ATTEMPTS + 2))
        with pytest.raises(AttioError) as excinfo:
            _post(fake)
        assert fake.calls == MAX_ATTEMPTS
        # Attio's own message is surfaced verbatim, not swallowed.
        assert "upstream said no" in str(excinfo.value)

    def test_5xx_is_not_retried_for_a_non_idempotent_create(
        self, captured_delays: list[float]
    ) -> None:
        # A 5xx may have committed the write before the response was lost, so
        # replaying a create risks a duplicate record. Fail loudly instead.
        fake = FlakyAttio([500])
        with pytest.raises(AttioError):
            _post(fake, retry_server_faults=False)
        assert fake.calls == 1
        assert captured_delays == []

    @pytest.mark.parametrize("status", [400, 401, 404, 413, 422])
    def test_other_4xx_is_never_retried(self, status: int, captured_delays: list[float]) -> None:
        fake = FlakyAttio([status] * MAX_ATTEMPTS)
        with pytest.raises(AttioError):
            _post(fake)
        assert fake.calls == 1

    def test_401_message_points_at_the_api_key(self, captured_delays: list[float]) -> None:
        fake = FlakyAttio([401])
        with pytest.raises(AttioError) as excinfo:
            _post(fake)
        assert "ATTIO_API_KEY" in str(excinfo.value)


# The three call sites that POST-create a record Attio cannot deduplicate.
NON_IDEMPOTENT_CREATES = {
    ("POST", "/objects/companies/records"),
    ("POST", "/objects/people/records"),
    ("POST", "/notes"),
}


def _request_call_sites() -> list[tuple[str, str, dict[str, str]]]:
    """Every `self._request(...)` call site as (method, path, keywords)."""
    tree = ast.parse(Path(attio_client.__file__).read_text())
    sites: list[tuple[str, str, dict[str, str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "_request"):
            continue
        method_node, path_node = node.args[1], node.args[2]
        path = path_node.value if isinstance(path_node, ast.Constant) else ast.unparse(path_node)
        keywords = {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}
        sites.append((method_node.value, path, keywords))
    return sites


class TestRetryPolicySourceContract:
    """Pins *which* call sites opt out of 5xx retries.

    Checked by reading the module's AST rather than by driving each public
    method through a mock. A behavioural test can only prove that the flag
    works (TestRetryPolicy does that); it cannot prove that no fourth create
    site was later added without the flag, which is the regression that would
    silently start duplicating records. The AST sees all of them at once.
    """

    def test_exactly_the_three_creates_opt_out_of_5xx_retries(self) -> None:
        opted_out = {
            (method, path)
            for method, path, keywords in _request_call_sites()
            if keywords.get("retry_server_faults") == "False"
        }
        assert opted_out == NON_IDEMPOTENT_CREATES

    def test_every_other_call_site_keeps_the_retrying_default(self) -> None:
        for method, path, keywords in _request_call_sites():
            if (method, path) in NON_IDEMPOTENT_CREATES:
                continue
            # Reads (…/query) and updates (PUT upsert, PATCH by id) are
            # idempotent, so replaying them on a 5xx is safe and desirable.
            assert "retry_server_faults" not in keywords, (method, path)
            is_read = method == "POST" and path.endswith("/query")
            is_update = method in {"PUT", "PATCH"}
            assert is_read or is_update, (method, path)

    def test_all_creates_are_actually_reached_by_the_adapter(self) -> None:
        # Guards the test above from passing vacuously if a create is renamed.
        sites = {(method, path) for method, path, _ in _request_call_sites()}
        assert NON_IDEMPOTENT_CREATES <= sites
