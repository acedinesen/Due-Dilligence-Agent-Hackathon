# Parallel Build Plan — 3-Person Split

Goal: let 3 people build independently, then merge into one working prototype before the TechBBQ 2026 demo (Aug 26–27).

Read `docs/BUILD_PLAN.md` and `docs/data-extraction.md` first — this plan operationalizes both into three isolated workstreams. Where the two source docs disagree (see Phase 1), this plan states the resolved decision; **BUILD_PLAN.md wins on scope disputes.**

Each phase below is self-contained enough to hand to a fresh Claude session — it names the exact files, the exact docs to re-read, and a verification checklist.

---

## Phase 0 — Documentation Discovery (done, consolidated here)

**Sources read:** `docs/BUILD_PLAN.md`, `docs/data-extraction.md`, `README.md`, `app/models.py`, `app/agent.py`, `app/main.py`, `app/adapters/pdf_reader.py`, `app/adapters/supabase_store.py`, `app/config.py`, `app/firm_profiles.py`, `supabase/schema.sql`, `firm_profiles/generic_seed.json`, `tests/test_models.py`, `requirements.txt`, `railway.json`.

**Current state:**
- FastAPI skeleton exists and runs: `GET /health`, `POST /parse`, `POST /analyze?firm=...` (`app/main.py`).
- `ParsedDeck` / `Source` / `Finding` / `FounderQuestion` / `FirmProfile` models already exist in `app/models.py`. `Source` already enforces the evidence rule via a `model_validator` (external → needs `url`, deck → needs `page`) — do not remove this.
- `MockDiligenceAgent` (`app/agent.py`) is a placeholder that proves plumbing only. It implements a `DiligenceAgent` Protocol — the real agent must implement the same `async def analyze(self, deck: ParsedDeck, firm: FirmProfile | None) -> DiligenceReport` signature.
- `PdfReaderClient._normalize()` (`app/adapters/pdf_reader.py:25`) is the **only** place to change if the Railway PDF reader's JSON shape differs from the guessed `text`/`full_text`/`content` + `pages` shape.
- `SupabaseStore` (`app/adapters/supabase_store.py`) auto-disables when `SUPABASE_URL`/`SUPABASE_KEY` are unset — safe to develop without Supabase.
- No frontend exists yet. No `anthropic` package in `requirements.txt` yet.
- `app/config.py:14` has `anthropic_model: str = ""` — unset, needs a real default.

**Allowed APIs for the extraction agent (verified against the current Anthropic Python SDK docs this session):**
- Model id: `claude-sonnet-5` (set as the default in `app/config.py`; swap to `claude-opus-5` only if quality requires it close to demo time — this is a cost/quality dial, not a correctness issue).
- Structured JSON output: `client.messages.parse(model=..., max_tokens=..., messages=[...], output_format=SomeBaseModel)` → `response.parsed_output` is a validated instance. This is the current, non-deprecated pattern (not the old `output_format=` top-level param on `.create()`, and not hand-parsing free text).
- Web research: declare `{"type": "web_search_20260209", "name": "web_search"}` in `tools` (optionally `max_uses`, `allowed_domains`/`blocked_domains`). Results come back as a `web_search_tool_result` content block; on success `.content` is a **list** of `web_search_result` items, on failure `.content` is a **single error object** — branch on that shape before indexing, don't assume success.
- **Anti-pattern:** don't hand-roll HTTP calls to `api.anthropic.com` — use the official `anthropic` SDK (add it to `requirements.txt`). Don't invent tool names/params not listed above.
- **Known gap to verify live (not assumed):** the exact field names on a `web_search_result` item (title/url/snippet) were not enumerated in the docs consulted this session. Whoever implements Track B must print one real `web_search_tool_result` block during development and confirm field names before hardcoding access to them.

**Not for the build (per BUILD_PLAN.md "What Not to Build"):** multi-agent orchestration, vector DB, full IC memo generation, auth, tenant isolation, CRM/portfolio features, **automated investment scoring / composite scores / complex weighting formulas**.

---

## Phase 1 — Freeze the Shared Contract (prerequisite, ~30–60 min, whole team or one person)

This is the one piece that **cannot** be parallelized safely — all three tracks build against it. Do this first, commit it, then fork into Phase 2.

### Resolved scope decision

`docs/data-extraction.md` proposes a numerically weighted 8-category founder scorecard and an overall weighted composite score. `docs/BUILD_PLAN.md` explicitly forbids "automated investment scoring systems" and "complex weighting formulas" under **What Not to Build**.

**Decision (confirmed with the team): qualitative only.** Extract the same categories/pillars data-extraction.md identifies, but as `status` (`supported` / `questionable` / `red_flag` / `unknown`) + evidence text — no numeric weights, no per-category scores, no composite score anywhere in the schema. This keeps BUILD_PLAN's constraint intact and is faster to build correctly in two days.

### What to implement

Extend `app/models.py` (additive to what exists — keep `Source`, `Finding`, `FounderQuestion`, `FirmProfile`, `ParsedDeck`, `DeckPage` as-is):

```python
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
```

Update `MetricResult.name` to cover the remaining pillars from data-extraction.md's "Additional Metrics Worth Capturing":

```python
class MetricResult(BaseModel):
    name: Literal[
        "problem_validation", "traction", "business_model_clarity",
        "cap_table_legal", "ask_and_use_of_funds", "non_obvious_insight",
    ]
    status: Literal["supported", "questionable", "red_flag", "unknown"]
    summary: str
    sources: list[Source] = Field(default_factory=list)
```

Add an optional pillar tag to `Finding` so the dashboard can group/filter red flags by widget:

```python
    pillar: Literal[
        "tam", "competitors", "founder", "traction",
        "business_model", "legal", "ask", "other",
    ] | None = None
```

Replace `DiligenceReport`:

```python
class DiligenceReport(BaseModel):
    company_name: str | None = None
    overview: str
    tam_sam_som: TamSamSomBreakdown
    competitors: CompetitorAnalysis
    founder: FounderProfile
    additional_metrics: list[MetricResult] = Field(default_factory=list)
    key_findings: list[Finding] = Field(max_length=5)
    founder_questions: list[FounderQuestion] = Field(max_length=5)
```

### Verification checklist

- `python -c "import app.models"` imports cleanly.
- `pytest tests/test_models.py` still passes unchanged (the `Source` evidence-rule tests must not need edits).
- Add one fixture: hand-write a single valid `DiligenceReport` JSON (can be fictional data) and confirm `DiligenceReport.model_validate(fixture)` succeeds. **Save this fixture to `tests/fixtures/sample_report.json`** — Track C needs it immediately and Track B needs it as a target shape.
- Also hand-write one `ParsedDeck` fixture and save to `tests/fixtures/sample_deck.json` — Track B needs this so it never has to wait on Track A.

### Anti-pattern guards

- Do not add a numeric `score`, `weight`, or `composite_score` field anywhere — that reopens the scope conflict this phase just resolved.
- Do not rename or remove the existing `Source`/`Finding`/`FounderQuestion` fields that BUILD_PLAN's evidence rule and API examples already rely on.
- Once committed, this schema is frozen for the parallel phase. If a track discovers the schema doesn't fit mid-build, stop and re-sync with the other two — don't silently diverge.

---

## Phase 2 — Parallel Tracks (3 people, fully independent once Phase 1 is committed)

Each track can be handed to its own fresh chat session. Each only needs: this file, the fixtures from Phase 1, and its own section below.

### Track A — Ingestion ("pitch deck → readable data")

**Owner:** Person 1
**Files:** `app/adapters/pdf_reader.py`, `app/adapters/supabase_store.py`, `supabase/schema.sql`, `app/main.py` (`/parse`, `/analyze` only — don't touch the agent call)

**What to implement:**
1. Verify the real Railway PDF reader contract (BUILD_PLAN.md "Step 1"). POST an actual pitch deck PDF to `https://pdfreader-production-29d1.up.railway.app` + `PDF_READER_PATH` (default `/parse`, confirm via env) and inspect the real JSON keys returned.
2. If the real shape differs from the guessed `text`/`full_text`/`content` + `pages` (str list or dict list with `page`/`page_number`, `text`/`content`), fix **only** `PdfReaderClient._normalize()` at `app/adapters/pdf_reader.py:25`. Do not change `ParsedDeck`'s field names — that's part of the frozen contract from Phase 1.
3. Wire Supabase (BUILD_PLAN.md "Step 2"): apply `supabase/schema.sql`, set `SUPABASE_URL`/`SUPABASE_KEY`, confirm `SupabaseStore.save()` (`app/adapters/supabase_store.py:19-43`) actually uploads to the `pitch-decks` bucket and inserts into `pitch_decks`.
4. Optional, only if time allows: improve `full_text` readability (e.g., join pages as `## Slide {n}\n{text}` markdown) — content-only change, does not touch the schema.
5. Produce 2–3 real `ParsedDeck` JSON fixtures from actual sample decks and hand them to Track B (in addition to the hand-written Phase 1 fixture).

**Docs to cite:** `docs/BUILD_PLAN.md` §"Step 1", §"Step 2"; `README.md` "Base API" and "Existing PDF reader" sections; `app/adapters/pdf_reader.py`; `supabase/schema.sql`.

**Verification:**
- `curl -F file=@sample.pdf https://pdfreader-production-29d1.up.railway.app/parse` returns 200 with usable text.
- Local `POST /parse` on a running `uvicorn app.main:app` returns a valid `ParsedDeck`.
- With `SUPABASE_URL`/`SUPABASE_KEY` set, `POST /analyze` returns a non-null `deck_id` and a row appears in `pitch_decks`.

**Anti-pattern guards:** don't invent new Railway endpoints; don't add auth/tenant logic (explicitly out of scope); don't rename `ParsedDeck`/`DeckPage` fields.

---

### Track B — Extraction & Scoring Agent ("structure data for the dashboard")

**Owner:** Person 2
**Files:** new `app/diligence.py` (replaces `app/agent.py`'s `MockDiligenceAgent` usage), `app/config.py`, `requirements.txt`

**What to implement:**
1. `pip install anthropic`, add it to `requirements.txt`, and set `app/config.py:14` `anthropic_model` default to `"claude-sonnet-5"`.
2. Implement `ClaudeDiligenceAgent` matching the existing `DiligenceAgent` Protocol in `app/agent.py` (`async def analyze(self, deck: ParsedDeck, firm: FirmProfile | None) -> DiligenceReport`), in a new `app/diligence.py`.
3. Two-call pattern (avoids mixing forced structured output with an open-ended tool loop in one call):
   - **Research call:** loop `client.messages.create(...)` with `tools=[{"type": "web_search_20260209", "name": "web_search"}]` and a system prompt scoped to BUILD_PLAN.md §6 (TAM credibility, competitor landscape, founder background) — instruct Claude to only assert externally-sourced claims it can back with a URL from a `web_search_tool_result`.
   - **Structured extraction call:** `client.messages.parse(model=..., messages=[deck text + research transcript], output_format=DiligenceReport)` → `response.parsed_output`.
4. **Known pitfall to guard against:** `output_format`'s auto-derived JSON schema captures structural types but not `Source`'s custom cross-field rule (external → needs `url`). `client.messages.parse` runs full pydantic validation client-side when building `parsed_output`, so a `pydantic.ValidationError` is possible if Claude emits an external source with no URL. Wrap the parse call and retry once with a corrective follow-up message before giving up.
5. Fold in `FirmProfile.criteria` (when present) as an emphasis note in the system prompt — must remain optional; the generic (no-firm) path must still work end-to-end (BUILD_PLAN.md §10).
6. Derive `key_findings` (≤5) and `founder_questions` (≤5) as part of the same structured call — don't add a third LLM call just for these, per BUILD_PLAN.md §5/§8's requirements (non-generic, evidence-traceable questions).
7. Develop and test entirely against `tests/fixtures/sample_deck.json` from Phase 1/Track A — do not block on a live Railway or Supabase connection.

**Docs to cite:** `docs/data-extraction.md` (this track's extraction spec — TAM/SAM/SOM, competitors, founder categories, additional metrics — read qualitatively per Phase 1's resolved decision, ignore its numeric weights); `docs/BUILD_PLAN.md` §5 (Evidence Rule), §6, §8; `app/agent.py` (Protocol to implement); `app/models.py` (the `Source` validator you must satisfy).

**Verification:**
- Extend `tests/test_models.py`-style tests to cover the new models from Phase 1.
- Run `ClaudeDiligenceAgent.analyze()` against the fixture deck(s); confirm output validates as `DiligenceReport`, has ≤5 findings, ≤5 questions, and every external `Source` carries a real, non-hallucinated-looking URL (spot-check manually).
- Confirm the generic (`firm=None`) path produces a full report with no firm profile passed.

**Anti-pattern guards:** no multi-agent orchestration, no vector DB (BUILD_PLAN "What Not to Build"); don't hand-parse free-text JSON when `client.messages.parse` exists; don't skip the evidence-rule retry; don't assume `web_search_result` field names — print one real block first (see Phase 0's "Known gap").

---

### Track C — Dashboard ("visualize the found data")

**Owner:** Person 3
**Files:** new top-level `frontend/` (or `web/`) directory — a separate deployable unit, not entangled with the Python backend.

**What to implement:**
1. Build entirely against `tests/fixtures/sample_report.json` from Phase 1 — never blocked on Track A or B being finished.
2. Screens/widgets, one per pillar in `DiligenceReport` (per BUILD_PLAN.md "Step 7" and data-extraction.md's "Suggested Dashboard Schema"):
   - Upload pitch deck → `POST /analyze?firm=...`
   - Company overview
   - TAM/SAM/SOM card: stated values, methodology tag, `som_pct_of_sam_flagged` badge
   - Competitors table: name, funding info, differentiation, direct/adjacent flag, verified badge, plus `why_now_why_us`
   - Founder panel: 8 categories with status badges + `founder_market_fit`
   - Additional metrics panel (the 6 `MetricResult` entries)
   - Key Findings/Risks list (≤5, `risk_level` + `pillar` tag)
   - Founder Questions (≤5, one-click copy)
   - Evidence drill-down: every pillar's `sources` list, rendered as deck-page references or external links
3. No numeric score/gauge anywhere — Phase 1 deliberately excluded composite scoring; render qualitative status badges (`supported`/`questionable`/`red_flag`/`unknown`) instead.
4. Framework choice is Person 3's call given the 2-day clock; whatever is chosen, keep it its own directory/package so backend and frontend stay independently deployable, matching the adapter-isolation approach already used in `app/adapters/`.
5. Use the `dataviz` skill for status-badge/stat-tile/table visual design consistency.

**Docs to cite:** `docs/BUILD_PLAN.md` §"Step 7"; `docs/data-extraction.md` "Suggested Dashboard Schema"; `README.md` "Base API" (`POST /analyze?firm=...` → `{deck_id, report}` shape).

**Verification:**
- Dashboard renders fully against the fixture JSON with zero backend running.
- Every pillar's evidence drill-down opens and shows its `sources`.
- Findings/questions lists never render more than 5 items even if fed a malformed fixture (defensive slicing, don't trust upstream blindly).

**Anti-pattern guards:** no login/auth; no portfolio/CRM features; no score gauges/numbers (explicitly excluded in Phase 1).

---

## Phase 3 — Synthesis (whole team, short session)

1. Merge Track A's adapter changes, Track B's `app/diligence.py` + any `app/models.py` follow-ups, and Track C's `frontend/` directory.
2. In `app/main.py`, swap `MockDiligenceAgent` → `ClaudeDiligenceAgent` (`app/main.py:15`); firm-profile loading (`app/main.py:41`) is unchanged.
3. Point Track C's upload form / fetch calls at the real backend (local `uvicorn` first, then the Railway deployment URL).
4. Run one real pitch deck end-to-end: upload → `/analyze` (generic_seed) → dashboard renders. If time remains, demo the optional firm-profile diff (BUILD_PLAN.md "Step 8") by adding a second firm JSON to `firm_profiles/` and re-running the same deck.
5. Update `README.md`'s "Next code task" section (currently says to replace `MockDiligenceAgent`) to reflect the shipped state.

**Verification checklist (final gate before demo):**
- `pytest` passes across all new and existing tests.
- Full flow works end-to-end with a real sample deck, in front of the group, before the demo slot.
- `GET /health` still returns `{"ok": true}` on the Railway deployment.
- No numeric composite score, weighting formula, auth, or multi-agent orchestration snuck back in during merge (re-check against BUILD_PLAN's "What Not to Build" and Phase 1's resolved decision).

**Anti-pattern guard:** if a mismatch between tracks surfaces during merge, fix the producer or consumer — don't silently patch the frozen Phase 1 schema without a 2-minute sync with whoever else depends on it.

---

## Optional — Demo Framing

`docs/judges_profiles.md` profiles the likely TechBBQ 2026 judges (Kellezi/byFounders, Milo, den Teuling). Not a build task, but worth 5 minutes before the demo: Kellezi responds to technical depth and authenticity over polish, den Teuling to provable ROI over hype — frame the walkthrough (not the product itself) accordingly.
