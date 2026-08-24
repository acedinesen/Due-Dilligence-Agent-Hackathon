from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.models import DiligenceReport, FounderSummary, Source

ATTIO_BASE_URL = "https://api.attio.com/v2"

# An Attio API key is exactly 64 characters. Checking it up front turns an
# opaque 401 into a clear local error — auth is evaluated before body
# validation, so a bad key otherwise masks every other problem.
ATTIO_KEY_LENGTH = 64

# POST /v2/notes answers 413 on an oversized body. The ceiling is undocumented;
# probed live against the workspace, 200KB of content was accepted and 500KB was
# rejected ("Note content exceeds the maximum supported size when encoded").
# This sits far below that, and we truncate rather than fail.
NOTE_CONTENT_MAX_BYTES = 60_000
NOTE_TRUNCATION_MARKER = "\n\n_[truncated]_\n"

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 1.0
RETRY_AFTER_CAP_SECONDS = 30.0


class AttioError(RuntimeError):
    """A non-retryable Attio API failure.

    The exception text always carries Attio's own ``message`` verbatim so a
    developer sees exactly what the API objected to.
    """


# --------------------------------------------------------------------------
# value coercion helpers
# --------------------------------------------------------------------------


def _normalize_domain(url: object) -> str | None:
    """Reduce a website URL to the bare domain Attio's `domain` type stores.

    Attio trims paths and query strings itself, but whether it also strips the
    scheme is unverified — so we normalize client-side and send a clean host.
    """
    raw = str(url or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw if "//" in raw else f"//{raw}")
    # .hostname already lowercases and strips userinfo, port and IPv6 brackets.
    # Doing it by hand with split(":") mangles a bracketed IPv6 literal into "[",
    # which would then be written into the *unique* `domains` attribute.
    host = (parsed.hostname or "").strip().removeprefix("www.")
    return host or None


def _split_person_name(full_name: str) -> dict[str, str]:
    """Build Attio's `personal-name` object form.

    All three keys are required together. We must use the object form rather
    than the bare-string shorthand, because on *write* a bare string is parsed
    as ``"Last, First"`` — so "Jane Doe" would land as a last name only.
    """
    cleaned = " ".join(str(full_name).split())
    parts = cleaned.split(" ")
    return {
        "first_name": parts[0] if parts else "",
        "last_name": " ".join(parts[1:]),  # empty for a single-token name
        "full_name": cleaned,
    }


def _text(value: str) -> list[dict[str, str]]:
    return [{"value": value}]


def _company_reference(record_id: str) -> list[dict[str, str]]:
    return [{"target_object": "companies", "target_record_id": record_id}]


# --------------------------------------------------------------------------
# note body
# --------------------------------------------------------------------------


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def _format_sources(sources: list[Source]) -> list[str]:
    lines: list[str] = []
    for source in sources:
        if source.url:
            lines.append(f"  - {source.title} — {source.url}")
        elif source.page is not None:
            lines.append(f"  - {source.title} — deck p. {source.page}")
        else:
            lines.append(f"  - {source.title}")
    return lines


def _build_note_markdown(report: DiligenceReport) -> str:
    """Render the report body as markdown.

    Deliberately carries no score, weight or composite of any kind — the team
    decided against scoring, and this is the output layer where it would be
    tempting to reintroduce one.
    """
    company = report.company
    lines: list[str] = [f"# {company.name}", "", company.one_liner, ""]

    if company.website_url:
        lines += [f"Website: {company.website_url}", ""]

    lines += ["## Overview", "", report.overview, ""]

    market = report.tam_sam_som
    lines += ["## Market (TAM / SAM / SOM)", "", market.summary, ""]
    lines += [
        f"- TAM stated: {market.tam_stated or 'not stated'}",
        f"- SAM stated: {market.sam_stated or 'not stated'}",
        f"- SOM stated: {market.som_stated or 'not stated'}",
        f"- Sizing methodology: {market.tam_methodology}",
        f"- External validation present: {_yes_no(market.external_validation_present)}",
        f"- SOM-vs-SAM plausibility flagged: {_yes_no(market.som_pct_of_sam_flagged)}",
        "",
    ]
    lines += _format_sources(market.sources) + [""]

    rivals = report.competitors
    lines += ["## Competitors", "", rivals.summary, ""]
    if rivals.why_now_why_us:
        lines += [f"Why now / why us: {rivals.why_now_why_us}", ""]
    for rival in rivals.competitors:
        kind = "direct" if rival.is_direct else "indirect"
        verified = "externally verified" if rival.verified_externally else "unverified"
        detail = f"- **{rival.name}** ({kind}, {verified}) — {rival.differentiation_claimed}"
        if rival.funding_info:
            detail += f" Funding: {rival.funding_info}"
        lines.append(detail)
    if rivals.competitors:
        lines.append("")
    if rivals.missing_direct_competitor_flag:
        lines += ["- Flagged: no credible direct competitor named in the deck.", ""]
    lines += _format_sources(rivals.sources) + [""]

    profile = report.founder_profile
    lines += ["## Founder profile", "", profile.summary, ""]
    lines += [f"- Founder–market fit: {profile.founder_market_fit}", ""]
    for note in profile.categories:
        label = note.category.replace("_", " ")
        lines.append(f"- **{label}** ({note.status}) — {note.evidence}")
    if profile.categories:
        lines.append("")
    lines += _format_sources(profile.sources) + [""]

    if report.founders:
        lines += ["## Founders", ""]
        for founder in report.founders:
            entry = f"- **{founder.name}** — {founder.bio_one_liner}"
            if founder.linkedin_url:
                entry += f" ([LinkedIn]({founder.linkedin_url}))"
            lines.append(entry)
        lines.append("")

    if report.additional_metrics:
        lines += ["## Additional metrics", ""]
        for metric in report.additional_metrics:
            label = metric.name.replace("_", " ")
            lines.append(f"- **{label}** ({metric.status}) — {metric.summary}")
            lines += _format_sources(metric.sources)
        lines.append("")

    lines += ["## Key findings", ""]
    for finding in report.key_findings:
        lines += [
            f"### {finding.title}",
            "",
            f"- Risk level: {finding.risk_level}",
            f"- Pillar: {finding.pillar or 'unspecified'}",
            "",
            finding.explanation,
            "",
            f"Why it matters: {finding.why_it_matters}",
            "",
        ]
        source_lines = _format_sources(finding.sources)
        if source_lines:
            lines += ["Sources:"] + source_lines + [""]

    lines += ["## Questions for the founders", ""]
    for item in report.founder_questions:
        entry = f"- {item.question}"
        if item.based_on_finding_ids:
            entry += f" (from: {', '.join(item.based_on_finding_ids)})"
        lines.append(entry)

    return "\n".join(lines).strip() + "\n"


def _truncate_note(content: str) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) <= NOTE_CONTENT_MAX_BYTES:
        return content

    budget = NOTE_CONTENT_MAX_BYTES - len(NOTE_TRUNCATION_MARKER.encode("utf-8"))
    kept = encoded[:budget].decode("utf-8", errors="ignore")
    return kept + NOTE_TRUNCATION_MARKER


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


def _api_key() -> str:
    key = (settings.attio_api_key or "").strip()
    if not key:
        raise AttioError(
            "ATTIO_API_KEY is not set — add it to .env (see .env.example). "
            "Attio rejects the request before validating anything else."
        )
    if len(key) != ATTIO_KEY_LENGTH:
        raise AttioError(
            f"ATTIO_API_KEY looks malformed: expected exactly {ATTIO_KEY_LENGTH} "
            f"characters, got {len(key)}. Re-copy the key from Attio "
            "(Workspace settings → Developers → Access tokens)."
        )
    return key


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Attio sends `Retry-After` as an HTTP-date, not a seconds integer."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:  # tolerate a plain-seconds value from an intermediary
            return min(max(float(raw), 0.0), RETRY_AFTER_CAP_SECONDS)
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delay = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(delay, 0.0), RETRY_AFTER_CAP_SECONDS)


def _describe_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    message = payload.get("message") or (response.text or "").strip()[:500]
    parts = [f"HTTP {response.status_code}"]
    if payload.get("type"):
        parts.append(f"type={payload['type']}")
    if payload.get("code"):
        parts.append(f"code={payload['code']}")

    detail = f"Attio {' '.join(parts)}: {message or '<no message in response>'}"

    if response.status_code == 401:
        detail += (
            " — check ATTIO_API_KEY: it must be exactly "
            f"{ATTIO_KEY_LENGTH} characters and still active in the workspace."
        )
    return detail


class AttioClient:
    """Thin adapter over Attio's REST API for one company + its founders.

    Idempotency, which Attio's uniqueness rules constrain sharply:

    * Companies with a website are upserted on ``domains`` — the only unique
      writable attribute on the object. Re-running updates the same record.
    * Companies without a website cannot be upserted (``matching_attribute``
      must be unique, and ``name`` is not; Attio forbids new unique custom
      attributes on companies). We query on name, then PATCH the first hit or
      create. Re-running updates the same record.
    * People have no unique attribute we can populate — ``email_addresses`` is
      the only one and ``FounderSummary`` carries no email. Rather than accept
      duplicate founders on every run, we query people by ``full_name`` and
      keep only a hit already linked to this company, then PATCH it. Verified
      live: the implicit ``records/query`` filter on a ``personal-name``
      attribute does match ``full_name`` from a bare string (unlike a *write*,
      where a bare string means "Last, First").
    * Notes have no upsert, so each run appends a fresh note. That is
      deliberate — the note is a dated artefact of one analysis run.
    """

    def __init__(self, base_url: str = ATTIO_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    # -- transport ---------------------------------------------------------

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        retry_server_faults: bool = True,
    ) -> dict | list:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_error: str = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = await client.request(method, url, params=params, json=json)

            # Success is 200 for creates and upserts alike, not 201.
            if response.status_code == 200:
                return response.json()["data"]

            # Attio documents that a 429'd request was *not* processed, so
            # replaying it is always safe. A 5xx carries no such guarantee: the
            # write may have committed before the response was lost. So callers
            # that POST-create a non-idempotent record (person, no-domain
            # company, note) pass retry_server_faults=False and surface the
            # fault instead of risking a duplicate.
            retryable = response.status_code == 429 or (
                retry_server_faults and response.status_code >= 500
            )
            last_error = _describe_error(response)
            if not retryable or attempt == MAX_ATTEMPTS:
                raise AttioError(f"{method} {path} failed — {last_error}")

            delay = _retry_after_seconds(response)
            if delay is None:
                delay = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            await asyncio.sleep(delay)

        raise AttioError(f"{method} {path} failed — {last_error}")

    # -- companies ---------------------------------------------------------

    async def save_company(
        self, client: httpx.AsyncClient, report: DiligenceReport
    ) -> tuple[str, str | None]:
        company = report.company
        values: dict[str, object] = {
            "name": _text(company.name),
            "description": _text(company.one_liner),
        }
        # `team` is intentionally never sent: an upsert replaces multiselect
        # values wholesale, which would wipe the workspace's existing team
        # list. Founders are linked from the person side via `company`.

        domain = _normalize_domain(company.website_url) if company.website_url else None

        if domain:
            values["domains"] = [domain]
            data = await self._request(
                client,
                "PUT",
                "/objects/companies/records",
                params={"matching_attribute": "domains"},
                json={"data": {"values": values}},
            )
        else:
            # No domain, so no upsert is possible. Omit the key entirely
            # rather than sending [] or null.
            existing_id = await self._find_company_by_name(client, company.name)
            if existing_id:
                data = await self._request(
                    client,
                    "PATCH",
                    f"/objects/companies/records/{existing_id}",
                    json={"data": {"values": values}},
                )
            else:
                data = await self._request(
                    client,
                    "POST",
                    "/objects/companies/records",
                    json={"data": {"values": values}},
                    retry_server_faults=False,
                )

        return data["id"]["record_id"], data.get("web_url")

    async def _find_company_by_name(
        self, client: httpx.AsyncClient, name: str
    ) -> str | None:
        data = await self._request(
            client,
            "POST",
            "/objects/companies/records/query",
            json={"filter": {"name": {"$eq": name}}, "limit": 1},
        )
        return data[0]["id"]["record_id"] if data else None

    # -- people ------------------------------------------------------------

    async def save_person(
        self, client: httpx.AsyncClient, founder: FounderSummary, company_id: str
    ) -> tuple[str, str | None]:
        values: dict[str, object] = {
            "name": [_split_person_name(founder.name)],
            "description": _text(founder.bio_one_liner),
            "company": _company_reference(company_id),
        }
        # Omit `linkedin` entirely when absent — never write "None" or "".
        if founder.linkedin_url:
            values["linkedin"] = _text(str(founder.linkedin_url))

        existing_id = await self._find_person(client, founder.name, company_id)
        if existing_id:
            data = await self._request(
                client,
                "PATCH",
                f"/objects/people/records/{existing_id}",
                json={"data": {"values": values}},
            )
        else:
            data = await self._request(
                client,
                "POST",
                "/objects/people/records",
                json={"data": {"values": values}},
                retry_server_faults=False,
            )
        return data["id"]["record_id"], data.get("web_url")

    async def _find_person(
        self, client: httpx.AsyncClient, name: str, company_id: str
    ) -> str | None:
        full_name = " ".join(str(name).split())
        data = await self._request(
            client,
            "POST",
            "/objects/people/records/query",
            json={"filter": {"name": {"full_name": {"$eq": full_name}}}, "limit": 25},
        )
        # Namesakes are common, so only reuse a person already linked to this
        # company; anyone else with the same name gets left alone.
        for record in data:
            links = record.get("values", {}).get("company") or []
            if any(link.get("target_record_id") == company_id for link in links):
                return record["id"]["record_id"]
        return None

    # -- notes -------------------------------------------------------------

    async def create_note(
        self, client: httpx.AsyncClient, company_id: str, report: DiligenceReport
    ) -> str:
        # The report body goes in a note, not in `description`: companies are
        # enrichment-populated, and a long body would not survive there.
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = await self._request(
            client,
            "POST",
            "/notes",
            json={
                "data": {
                    "parent_object": "companies",
                    "parent_record_id": company_id,
                    "title": f"Due diligence report — {report.company.name} ({stamp})",
                    "format": "markdown",
                    "content": _truncate_note(_build_note_markdown(report)),
                }
            },
            retry_server_faults=False,
        )
        return data["id"]["note_id"]


async def save_to_attio(report: DiligenceReport) -> str | None:
    """Persist a finished report to Attio; return the company's app URL.

    Creates or updates the company record, links a person record per founder,
    and attaches the report body as a markdown note. Raises `AttioError` on an
    auth or schema failure rather than swallowing it — this is the last step
    before the demo's payoff, so breakage must be visible now, not live.
    """
    key = _api_key()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    attio = AttioClient()

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS, headers=headers
    ) as client:
        # The company must exist before any person references it — Attio
        # rejects a reference write whose target record is missing.
        company_id, web_url = await attio.save_company(client, report)

        for founder in report.founders:
            await attio.save_person(client, founder, company_id)

        await attio.create_note(client, company_id, report)

    return web_url
