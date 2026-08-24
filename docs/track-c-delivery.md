# Track C — Attio + Slack Delivery

> **You are one of 3 parallel workstreams** for the KeepItSimple due diligence agent, built for the TechBBQ 2026 hackathon (Aug 26–27). This file is self-contained — you shouldn't need to open anything else to start working — but the full picture lives in `docs/PARALLEL_BUILD_PLAN.md` if you want it. The other two tracks are:
> - **Track A** (`docs/track-a-drive-trigger.md`) — watches a Google Drive folder and triages inbound decks into relevant/review/not-relevant. Its output never reaches you directly.
> - **Track B** (`docs/track-b-deep-analysis.md`) — the Claude agent that does the actual TAM/competitors/founder research and produces the report you consume.
>
> You are the "payoff" moment of the demo: the Slack message that shows up after a deck was processed. You can build and fully test your track in isolation against a fixture report, without either of the other tracks existing yet.

---

## Project context

The product is a background pipeline: a pitch deck lands in a watched Drive folder, gets cheaply triaged, and — only if it passes (`relevant` or `review`) — gets a full deep-dive analysis. You're the last step.

```text
New file in Drive "Inbox/"                              ← Track A
        ↓
Cheap triage (relevant / review / not_relevant)          ← Track A
        ↓ (not_relevant decks never reach you — no Attio record, no Slack message)
Deep-dive: TAM / competitors / founder profile            ← Track B
        ↓
Save record to Attio                                     ← THIS IS YOU
Send Slack notification                                   ← THIS IS YOU
```

The triage gate matters to you specifically: **you are never called for a `not_relevant` deck.** That gating logic lives in the whole-team pipeline wiring (Phase 3), not in your code — your two functions should just assume "I've been given a report, deliver it," and not know or care about the triage flag.

---

## What you're building

Two functions, each taking a finished `DiligenceReport` and doing one delivery job:

- `save_to_attio(report: DiligenceReport) -> None` (or return the created record id if useful) — creates/updates a record in the team's Attio CRM.
- `send_slack_notification(report: DiligenceReport) -> None` — posts a Slack message with exactly these four things:
  1. Company name
  2. One-liner about it
  3. Founder bio (one-liner), with a link to LinkedIn
  4. Link to the company website

---

## Verified facts you need — read before writing any integration code

**There is no MCP tool for either Slack or Attio in this session** (checked directly against the live tool list): Slack and Gmail both only expose an OAuth handshake (`authenticate`/`complete_authentication`), and there's no Attio connector at all. That means:

- **Slack:** you must call Slack's own API. For hackathon speed, use an **Incoming Webhook** — one URL, no bot token, no OAuth scopes to configure — unless the team already has a Slack app with a bot token set up (check with the team before building your own from scratch). Load the `slack:slack-messaging` and `slack:block-kit` skills at build time for the exact current request/payload shape — don't guess the JSON structure from memory. Use Block Kit (a header block for the company name, section blocks for the rest) rather than a single unformatted text string — it's a small amount of extra structure for a much more legible demo message.
- **Attio:** you must call Attio's REST API directly with an API key. **Fetch `https://developers.attio.com`'s current docs before writing any code** — do not invent endpoint paths, auth header shapes, or field/object names. Confirm: how to authenticate, how to create or update a company record, and whether/how to attach a linked person record for the founder(s). This is a genuine unknown in this plan — verify it live, don't assume it matches some other CRM's API shape.

---

## Frozen data contract

This is the full shared schema, frozen by the team. Track B produces this; you only consume it — you should never need to modify these classes, but read them all, since your two functions need to reach into several parts of it (`company`, `founders`, and arguably `overview` / `key_findings` if you want to enrich the Attio record beyond the bare minimum).

```python
# app/models.py — target state, owned/produced by Track B. Reference only.

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class DeckPage(BaseModel):
    page: int
    text: str


class ParsedDeck(BaseModel):
    filename: str
    full_text: str
    pages: list[DeckPage] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class SourceType(str, Enum):
    DECK = "deck"
    EXTERNAL = "external"


class Source(BaseModel):
    type: SourceType
    title: str
    evidence: str
    page: int | None = None
    url: HttpUrl | None = None

    @model_validator(mode="after")
    def external_sources_need_links(self):
        if self.type == SourceType.EXTERNAL and self.url is None:
            raise ValueError("Every external source must include a URL")
        if self.type == SourceType.DECK and self.page is None:
            raise ValueError("Deck evidence must include a page number")
        return self


class TamSamSomBreakdown(BaseModel):
    tam_stated: str | None = None
    tam_methodology: Literal["top_down", "bottom_up", "both", "unclear"] = "unclear"
    sam_stated: str | None = None
    som_stated: str | None = None
    som_pct_of_sam_flagged: bool = False
    external_validation_present: bool = False
    summary: str
    sources: list[Source] = Field(default_factory=list)


class Competitor(BaseModel):
    name: str
    funding_info: str | None = None
    differentiation_claimed: str
    is_direct: bool
    verified_externally: bool


class CompetitorAnalysis(BaseModel):
    competitors: list[Competitor] = Field(default_factory=list)
    why_now_why_us: str | None = None
    missing_direct_competitor_flag: bool = False
    summary: str
    sources: list[Source] = Field(default_factory=list)


FounderCategory = Literal[
    "industry_experience", "vision_strategy", "track_record",
    "learning_agility", "team_leadership", "network_strength",
    "resilience", "execution_strength",
]


class FounderCategoryNote(BaseModel):
    category: FounderCategory
    status: Literal["supported", "questionable", "red_flag", "unknown"]
    evidence: str


class FounderProfile(BaseModel):
    categories: list[FounderCategoryNote]
    founder_market_fit: Literal["strong", "moderate", "weak", "unclear"]
    summary: str
    sources: list[Source] = Field(default_factory=list)


class MetricResult(BaseModel):
    name: Literal[
        "problem_validation", "traction", "business_model_clarity",
        "cap_table_legal", "ask_and_use_of_funds", "non_obvious_insight",
    ]
    status: Literal["supported", "questionable", "red_flag", "unknown"]
    summary: str
    sources: list[Source] = Field(default_factory=list)


class Finding(BaseModel):
    id: str
    title: str
    risk_level: Literal["low", "medium", "high"]
    pillar: Literal[
        "tam", "competitors", "founder", "traction",
        "business_model", "legal", "ask", "other",
    ] | None = None
    explanation: str
    why_it_matters: str
    sources: list[Source] = Field(default_factory=list)


class FounderQuestion(BaseModel):
    question: str
    based_on_finding_ids: list[str] = Field(default_factory=list)


class CompanySummary(BaseModel):
    name: str
    one_liner: str
    website_url: HttpUrl | None = None


class FounderSummary(BaseModel):
    name: str
    bio_one_liner: str
    linkedin_url: HttpUrl | None = None


class DiligenceReport(BaseModel):
    company: CompanySummary
    overview: str
    tam_sam_som: TamSamSomBreakdown
    competitors: CompetitorAnalysis
    founder_profile: FounderProfile
    founders: list[FounderSummary] = Field(default_factory=list)
    additional_metrics: list[MetricResult] = Field(default_factory=list)
    key_findings: list[Finding] = Field(max_length=5)
    founder_questions: list[FounderQuestion] = Field(max_length=5)
```

**The four Slack fields map directly to:**
1. Company name → `report.company.name`
2. One-liner → `report.company.one_liner`
3. Founder bio + LinkedIn → loop over `report.founders`, each a `FounderSummary` with `.bio_one_liner` and `.linkedin_url` (which may be `None`)
4. Website link → `report.company.website_url` (may be `None`)

**Handle `None` gracefully, always.** Track B is explicitly instructed to leave `website_url`/`linkedin_url` as `None` rather than fabricate a URL when research comes up empty. Your Slack message must render sensibly either way — e.g. show the founder's bio without a hyperlink, and omit the website line entirely, rather than printing a broken link or a literal "None".

---

## What to implement

**Files:** new `app/adapters/attio_client.py`, new `app/adapters/slack_notifier.py`.

1. **`app/adapters/attio_client.py`** — `save_to_attio(report: DiligenceReport) -> str | None`. Before writing this, fetch Attio's current API docs and confirm the exact request shape for creating/updating a company record (and, if it fits cleanly, a linked person record per founder). Map `report.company` (and `report.founders`) onto whatever object/attribute names Attio's docs specify — don't guess.
2. **`app/adapters/slack_notifier.py`** — `send_slack_notification(report: DiligenceReport) -> None`. Post the four fields above via an Incoming Webhook (or `chat.postMessage` if the team already has a bot token) using Block Kit for structure. Load `slack:block-kit` / `slack:slack-messaging` / `slack:slack-api` at build time for the exact payload.
3. Both functions should raise (or log loudly) on an auth or schema error rather than silently swallowing it — this is the last step before the demo's payoff moment, so failures need to be visible during development, not discovered live.
4. Keep both functions **triage-agnostic** — don't put any `if flag == "relevant"` branching inside them. That gating already happened upstream (Track A + the whole-team pipeline wiring); by the time either of your functions is called, the answer is always "yes, deliver this."

---

## Docs to cite

- `slack:block-kit`, `slack:slack-messaging`, `slack:slack-api` skills — load these at build time for the current Slack payload shapes; don't rely on memory here.
- Attio's live developer docs (`https://developers.attio.com`) — fetch at build time; this is a genuine unverified gap in this plan.

---

## Fixture (build against this — don't block on Track A or B)

Create `tests/fixtures/sample_report.json` if it doesn't already exist — a hand-written, realistic-but-fictional `DiligenceReport` that validates against the schema above, with **all four Slack fields populated** (including at least one `FounderSummary` with a real-looking LinkedIn URL and a `company.website_url`). Also create a second variant, or just test manually, with `website_url: null` and a founder with `linkedin_url: null`, to make sure your rendering handles the missing-link case correctly. If Track B has already produced a real fixture from their own output, prefer that over a hand-written one — it'll be closer to what you'll actually receive.

---

## Verification checklist

- Posting the fixture through `send_slack_notification` produces a real message in the team's Slack channel, correctly rendering all 4 fields, including the graceful-missing-link case.
- `save_to_attio` against the same fixture produces a real, inspectable record in the team's Attio workspace.
- Both functions fail loudly (a raised exception or a clearly logged error) on a bad API key or malformed request — test this deliberately once, don't just assume error handling works.

## Anti-pattern guards

- Don't build a bespoke Slack bot with interactive components, slash commands, or OAuth flows — a webhook post is enough for a one-way notification.
- Don't invent Attio field/object names before checking the docs.
- No numeric score anywhere in the Slack message or Attio record — the team already decided against composite/weighted scoring for this product; don't reintroduce it at the output layer.
- Don't put triage-flag branching logic inside your delivery functions — keep them pure "given a report, deliver it."
- Don't print a raw `None` into the Slack message or Attio record when a URL is missing — omit that line/field instead.

## Handoff

You build and test entirely against `tests/fixtures/sample_report.json` — you don't need Track A's Drive polling or Track B's live Claude calls to exist. When Track B is done, the only integration point is the whole-team pipeline (Phase 3) calling your two functions right after `diligence_agent.analyze(...)` returns, for any deck flagged `relevant` or `review`.
