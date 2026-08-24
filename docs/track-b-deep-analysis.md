# Track B — Deep-Dive Analysis Agent

> **You are one of 3 parallel workstreams** for the KeepItSimple due diligence agent, built for the TechBBQ 2026 hackathon (Aug 26–27). This file is self-contained — you shouldn't need to open anything else to start working — but the full picture lives in `docs/PARALLEL_BUILD_PLAN.md` if you want it. The other two tracks are:
> - **Track A** (`docs/track-a-drive-trigger.md`) — watches a Google Drive folder, runs a cheap triage pass, and hands you a parsed deck only for decks flagged `relevant` or `review`. You do not build this; you receive its output.
> - **Track C** (`docs/track-c-delivery.md`) — takes your output and pushes it to Attio + Slack. You do not build this; you produce what it consumes.
>
> You are the core "brain" of the product. You can build and fully test your track in isolation against a fixture deck, without either of the other tracks existing yet.

---

## Project context

The product is a background pipeline: a pitch deck lands in a watched Drive folder, gets cheaply triaged, and — only if it passes (`relevant` or `review`) — reaches you.

```text
New file in Drive "Inbox/"                              ← Track A
        ↓
Parse PDF, cheap triage (relevant / review / not_relevant) ← Track A
        ↓ (not_relevant decks never reach you)
Move file to the matching Drive folder                   ← Track A
        ↓
DEEP-DIVE: TAM / competitors / founder profile,           ← THIS IS YOU
  vs. pre-selected firm criteria, evidence-ruled,
  web-search-grounded
        ↓
Save record to Attio + Slack notification                ← Track C
  (company name, one-liner, founder bio + LinkedIn, website)
```

You are functionally the same "extraction and scoring agent" that a due-diligence dashboard product would need — the only thing that changed when this pivoted from a web-dashboard UI to a Gmail/Drive/Slack pipeline is *who calls you and what happens to your output*. Your actual research/analysis job is unchanged.

---

## What you're building

`ClaudeDiligenceAgent.analyze(deck: ParsedDeck, firm: FirmProfile | None) -> DiligenceReport` — given a parsed pitch deck and (optionally) a firm's pre-selected investment criteria, research and produce a structured, evidence-backed diligence report: TAM/SAM/SOM credibility, competitive landscape, founder profile, a handful of additional metrics, up to 5 key findings/risks, and up to 5 non-generic founder questions.

**New requirement versus the original due-diligence spec:** you must also populate a short company summary (name, one-liner, website URL) and per-founder summaries (bio one-liner, LinkedIn URL) — Track C needs these verbatim for the Slack notification and Attio record. If you can't find a website or LinkedIn URL via research, leave it `None` — never guess a URL.

---

## Verified facts you need (checked live against the current Anthropic Python SDK docs this session — don't rely on your own training-data priors here, the API has moved since)

- **Model id:** `claude-sonnet-5`. Set it as the default for `anthropic_model` in `app/config.py` (currently `""`).
- **Structured JSON output — the correct current pattern:**
  ```python
  response = client.messages.parse(
      model="claude-sonnet-5",
      max_tokens=16000,
      messages=[...],
      output_format=DiligenceReport,   # your pydantic model
  )
  report = response.parsed_output      # validated instance
  ```
  This is the current, non-deprecated pattern. Do **not** use the old top-level `output_format=` param on `.create()`, and do not hand-parse free-text JSON out of a plain text response.
- **Web research tool:** declare `{"type": "web_search_20260209", "name": "web_search"}` in `tools` (optionally `max_uses`, `allowed_domains`/`blocked_domains`). Results arrive as a `web_search_tool_result` content block: on success `.content` is a **list** of `web_search_result` items; on failure `.content` is a **single error object**. Branch on that shape before indexing — don't assume success.
- **Known gap — verify live, don't assume:** the exact field names on a `web_search_result` item (title/url/snippet) weren't enumerated in the docs checked this session. Print one real `web_search_tool_result` block during your own development and confirm field names before hardcoding access to them.
- **Two-call pattern (recommended over trying to force structured output and open-ended tool use in the same call):**
  1. **Research call** — `client.messages.create(..., tools=[{"type": "web_search_20260209", "name": "web_search"}])`, looped until `stop_reason == "end_turn"`, with a system prompt scoped to TAM credibility, competitor landscape, founder background, company website, and founder LinkedIn URLs.
  2. **Structured extraction call** — `client.messages.parse(..., output_format=DiligenceReport)`, fed the deck text + the research transcript, no tools.
- **Pitfall specific to this schema:** `client.messages.parse` runs full pydantic validation client-side when building `parsed_output`, including `Source`'s custom cross-field validator (external sources need a `url`, deck sources need a `page`). The auto-derived JSON schema Claude is constrained to doesn't capture that custom rule, so a `pydantic.ValidationError` is a real possibility if Claude emits an external source with no URL. Wrap the parse call in try/except and retry once with a corrective follow-up message before giving up.
- **Anti-pattern:** don't hand-roll HTTP calls to the Anthropic API — use the official `anthropic` SDK. `pip install anthropic` and add it to `requirements.txt` (it's not there yet).

---

## Frozen data contract

This is the full shared schema, frozen by the team. You are the primary owner/producer of `DiligenceReport` — if you find during implementation that a field genuinely doesn't fit, stop and sync with whoever owns Track C (they're the consumer) before changing it; don't silently diverge.

```python
# app/models.py — target state. Apply these changes yourself if no one has yet;
# don't block waiting for someone else to do it.

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
    som_pct_of_sam_flagged: bool = False  # True if outside the credible ~1-15% SOM/SAM range, or not derivable
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


class FirmProfile(BaseModel):
    id: str
    name: str
    criteria: list[str] = Field(default_factory=list)
```

**Scope decision already resolved — don't reopen it:** `docs/data-extraction.md` (your main content spec) proposes a numerically weighted 8-category founder scorecard and an overall composite score. `docs/BUILD_PLAN.md` explicitly forbids "automated investment scoring systems" and "complex weighting formulas." The team already resolved this: **qualitative only** — the categories above are extracted as `status` (`supported`/`questionable`/`red_flag`/`unknown`) + evidence text, never as a number or weight. Do not add a `score`, `weight`, or `composite_score` field anywhere.

---

## What to implement

**Files:** new `app/diligence.py`, edits to `app/config.py` and `requirements.txt`.

1. `pip install anthropic`; add it to `requirements.txt`; set `app/config.py`'s `anthropic_model` default to `"claude-sonnet-5"`.
2. Implement `ClaudeDiligenceAgent` in `app/diligence.py`, matching the `DiligenceAgent` Protocol already defined in `app/agent.py`:
   ```python
   class DiligenceAgent(Protocol):
       async def analyze(self, deck: ParsedDeck, firm: FirmProfile | None) -> DiligenceReport: ...
   ```
   (This replaces `MockDiligenceAgent` from `app/agent.py`, which exists only to prove plumbing — read it once to see the shape it's replacing.)
3. Research call (web search, TAM/competitors/founder background per `docs/BUILD_PLAN.md` §6, **plus** company website and founder LinkedIn/bio — new for this pivot), then structured extraction call (`client.messages.parse(output_format=DiligenceReport)`).
4. Fold in `firm.criteria` (when `firm` is not `None`) as an emphasis note in the system prompt — must remain optional; a `None` firm must still produce a full report (`docs/BUILD_PLAN.md` §10).
5. Derive `key_findings` (≤5) and `founder_questions` (≤5) as part of the *same* structured call — don't add a third LLM call just for these. Findings must be genuinely material (BUILD_PLAN §5), questions must be non-generic and traceable to a specific finding or unresolved assumption (BUILD_PLAN §8 — bad: "who are your competitors?"; good: a question that cites a specific gap your research found).
6. If a founder's LinkedIn or the company's website can't be found via web search, leave the field `None` — never fabricate a URL. Track C's Slack message is built to handle a missing link gracefully; don't work around that by inventing one.
7. Develop entirely against a fixture — see below. Do not wait on Track A's live Drive polling to be working.

---

## Docs to cite

- `docs/data-extraction.md` — your content spec (TAM/SAM/SOM, competitors, 8 founder categories, additional metrics). Read it for *what to extract*; ignore its numeric weighting (see "Scope decision already resolved" above).
- `docs/BUILD_PLAN.md` §5 (Evidence Rule — every external claim needs a URL, every deck claim needs a page number), §6 (the three core research areas), §8 (founder question requirement — non-generic, evidence-traceable).
- `app/agent.py` — the Protocol you're implementing.
- `app/models.py` — the `Source` validator your output must satisfy.

---

## Fixture (build against this — don't block on Track A)

Create `tests/fixtures/sample_deck.json` if it doesn't already exist — a hand-written, realistic-but-fictional `ParsedDeck` (a filename, a few pages of plausible pitch-deck text, a full_text field). Use it as your only input during development. If someone else already created it, use theirs — don't create a second, divergent one.

---

## Verification checklist

- `ClaudeDiligenceAgent.analyze()` against the fixture deck produces output that validates as `DiligenceReport` (`DiligenceReport.model_validate(...)` succeeds).
- ≤5 `key_findings`, ≤5 `founder_questions`.
- Every `Source` with `type == "external"` carries a URL that looks real (spot-check a couple manually — web search should ground these, but eyeball it given time pressure).
- `company.one_liner` is populated; `company.website_url` and at least one `FounderSummary.linkedin_url` are populated when the fixture/real deck's company is findable via search, and are cleanly `None` (not an empty string, not a fabricated URL) when not.
- Runs to completion with `firm=None` as well as with a real `FirmProfile`.

## Anti-pattern guards

- No multi-agent orchestration beyond the two-call pattern above; no vector DB (BUILD_PLAN "What Not to Build").
- No numeric score/weight/composite anywhere in your output — resolved decision, see above.
- Don't hand-parse free-text JSON when `client.messages.parse` exists.
- Don't skip the evidence-rule retry-on-validation-failure step.
- Don't assume `web_search_result` field names — print one real block first.
- Don't fabricate a website or LinkedIn URL when research comes up empty — `None` is the correct, honest answer.

## Handoff

You build and test entirely against `tests/fixtures/sample_deck.json` — you don't need Track A's Drive polling or Track C's Attio/Slack code to exist. Your output, a `DiligenceReport`, is exactly what Track C consumes — if you want to hand them something concrete before they've seen your real code, save one of your own real outputs to `tests/fixtures/sample_report.json` so they can build against real-shaped data instead of hand-written fiction.
