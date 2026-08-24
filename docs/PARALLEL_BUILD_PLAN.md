# Parallel Build Plan — 3-Person Split (v2: event-driven pipeline)

**Supersedes the earlier upload+dashboard architecture.** The product is now a background pipeline, not a UI: a pitch deck lands in a watched Google Drive folder (standing in for "received a pitch-deck email" — see Phase 0), gets triaged, filed, deep-analyzed, and pushed to Attio + Slack with no human in the loop until the Slack ping.

```text
New file in Drive "Inbox/" (simulates: pitch-deck email received)
        ↓
Parse PDF (existing Railway PDF reader adapter — unchanged)
        ↓
Triage agent: relevant / review / not_relevant  ──not_relevant──▶ move to Not-Relevant/, STOP
        ↓ relevant or review
Move file to Relevant/ or Review/  (Google Drive = the "flag")
        ↓
Deep-dive agent: TAM / competitors / founder profile, vs. pre-selected firm criteria
        ↓
Save record to Attio
        ↓
Slack notification: company name, one-liner, founder bio one-liner + LinkedIn, website link
```

Read `docs/BUILD_PLAN.md` and `docs/data-extraction.md` first — most of the analysis logic they describe is unchanged; only the trigger and the output surface changed. Where they disagree on scope, BUILD_PLAN.md wins (see Phase 1's resolved decision, carried over from v1 and still in force).

Each phase below is self-contained enough to hand to a fresh Claude session.

---

## Phase 0 — Documentation Discovery (consolidated)

**Sources read this round:** the v1 plan (superseded), the live MCP tool schemas for `mcp__claude_ai_Google_Drive__*`, and the deferred-tool listing for Gmail/Slack in this session.

**Resolved architecture decisions (confirmed with the team):**
1. **Trigger substrate is Google Drive, not the real Gmail API.** This session's Gmail MCP connector exposes only `authenticate`/`complete_authentication` — no read/list/label tools. Building real Gmail push notifications (Cloud Pub/Sub + domain verification) is not a two-day hackathon task. Per explicit instruction, the whole receive→triage→flag loop runs against Drive folders instead: dropping a PDF into `Inbox/` simulates "a pitch-deck email arrived." Wiring a real Gmail-to-Drive attachment forwarder is future work, out of scope here.
2. **Triage gates the pipeline.** `not_relevant` → the file is filed and the pipeline stops (no deep analysis, no Attio, no Slack). `relevant` and `review` both continue to the deep-dive agent, Attio, and Slack.
3. **Orchestration is a plain Python module in the existing repo** (no n8n) — a polling loop, since the existing FastAPI/Railway stack already deploys and no one has to learn a new tool mid-hackathon.

**Verified Google Drive MCP facts (`mcp__claude_ai_Google_Drive__*`) — this is the mechanism for "flag it in the respective folder":**
- `update_file(fileId, parentId, title?)` — **moving a file is done by setting `parentId` to the destination folder's id; it replaces the existing parent.** This is the exact "flag into relevant/review/not-relevant" primitive.
- `search_files(query)` supports a structured query language, including `parentId = '<folder_id>'` and `modifiedTime` comparisons — use this to poll `Inbox/` for new files (`parentId = '<inbox_id>'`).
- `create_file(title, parentId?, mimeType?)` — set `mimeType: "application/vnd.google-apps.folder"` to create the four folders (`Inbox`, `Relevant`, `Review`, `Not-Relevant`) if they don't already exist.
- `download_file_content` / `get_file_metadata` — fetch bytes + filename for a claimed file (schemas not re-verified here; pull them via ToolSearch when implementing Track A — same MCP server, same pattern as the four tools above).
- **No MCP tool moves/copies in bulk or watches for changes** — the poller must call `search_files` on an interval and diff against what it already claimed.

**Slack:** only `authenticate`/`complete_authentication` are exposed via MCP in this session — no `chat.postMessage`-equivalent tool. Real messages must go through Slack's own Web API (`chat.postMessage`) or, faster for a demo, an **Incoming Webhook** URL (no bot scopes to configure). Use the `slack:slack-messaging` / `slack:block-kit` / `slack:slack-api` skills at build time for the exact request shape — don't invent the payload format here.

**Attio:** no MCP tool available at all in this session. Track C must call Attio's REST API directly with an API key. **Do not invent endpoint paths or field names** — fetch Attio's current developer docs at build time (`https://developers.attio.com`) before writing the client code. This is a hard "verify, don't assume" gap.

**Anthropic API facts (unchanged from v1, still authoritative):**
- Model id: `claude-sonnet-5` — set as the default in `app/config.py` (`anthropic_model` is currently `""`).
- Structured JSON output: `client.messages.parse(model=..., max_tokens=..., messages=[...], output_format=SomeBaseModel)` → `response.parsed_output`.
- Web research: `{"type": "web_search_20260209", "name": "web_search"}` in `tools`. Result block `web_search_tool_result`: `.content` is a **list** on success, a **single error object** on failure — branch before indexing.
- **Anti-pattern:** don't hand-roll HTTP calls to the Anthropic API — use the `anthropic` SDK (not yet in `requirements.txt`, add it).
- **Known gap to verify live:** exact `web_search_result` field names — print one real block during Track B's development before hardcoding field access.

**Existing repo state that's still valid and reused as-is:**
- `ParsedDeck` / `Source` / `Finding` / `FirmProfile` / `DeckPage` in `app/models.py`, including the `Source` evidence-rule validator — keep.
- `PdfReaderClient` (`app/adapters/pdf_reader.py`) — the Railway PDF reader adapter is trigger-agnostic; reuse unchanged regardless of whether the deck bytes came from a file upload or a Drive download.
- `FirmProfile` / `firm_profiles/generic_seed.json` / `load_firm()` — this is exactly the "pre-selected criteria" the deep-dive agent should score against. No new concept needed; the pipeline just loads one fixed profile at startup instead of taking it as a request query param (there's no human making a per-request choice anymore). Use an env var, e.g. `PIPELINE_FIRM_PROFILE=generic_seed`.
- `app/main.py`'s `/parse` and `/analyze` HTTP endpoints — **keep them.** They're still the fastest way to manually test the deep-dive agent in isolation without running the whole Drive pipeline.

**Dropped from v1 (explicitly out of scope now):** the web dashboard / frontend track, Supabase as primary storage (Drive is now the file store; Supabase can stay as a nice-to-have audit log if time allows, but is not required for the demo).

**Still not for the build** (per BUILD_PLAN.md "What Not to Build" — unchanged): multi-agent orchestration beyond the two agents named here, vector DB, full IC memo generation, auth, tenant isolation, portfolio management, **automated composite/weighted scoring**.

---

## Phase 1 — Freeze the Shared Contract (prerequisite, ~30–45 min, whole team or one person)

Everything from v1's Phase 1 schema decision still holds — **qualitative status fields only, no numeric weights, no composite score** (BUILD_PLAN.md's "What Not to Build" vs. data-extraction.md's weighted scorecard was already resolved this way; nothing about the pipeline change reopens it).

### New: shared contracts for the delivery step

Slack and Attio both need a small, display-ready summary that isn't already in the deep-dive schema. Add to `app/models.py`:

```python
class FounderSummary(BaseModel):
    name: str
    bio_one_liner: str
    linkedin_url: HttpUrl | None = None


class CompanySummary(BaseModel):
    name: str
    one_liner: str
    website_url: HttpUrl | None = None


class TriageResult(BaseModel):
    flag: Literal["relevant", "review", "not_relevant"]
    reason: str
```

### Extend `DiligenceReport`

Rename the existing `founder: FounderProfile` field to `founder_profile` (avoids colliding with the new `founders` list — one is the qualitative 8-category scorecard, the other is the short bio+LinkedIn summary for notifications) and add `company` / `founders`:

```python
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

(`TamSamSomBreakdown`, `CompetitorAnalysis`, `FounderProfile`, `MetricResult`, `Finding.pillar` are unchanged from v1 — see git history of this file if you need the exact class bodies again, or just re-derive them from `docs/data-extraction.md` per the qualitative-only rule above.)

### Verification checklist

- `pytest tests/test_models.py` still passes.
- Update `tests/fixtures/sample_report.json` (or create it, if v1's Phase 1 wasn't executed yet) to include `company` and `founders` — this is what Track C builds against, so it must have realistic-looking fictional values for all four Slack fields (name, one-liner, founder bio, LinkedIn URL, website URL).
- Update/create `tests/fixtures/sample_deck.json` — same as v1, unchanged purpose (lets Track B build without a live Drive/Railway connection).
- `TriageResult.model_validate({"flag": "relevant", "reason": "..."})` succeeds; an invalid `flag` value raises.

### Anti-pattern guards

- No numeric fields anywhere in `CompanySummary`/`FounderSummary`/`TriageResult` — these are display/routing metadata, not scores.
- Don't let `founders`/`company` silently become the only place company info lives — `overview` and the pillar summaries still carry the substantive analysis; the summary models exist purely for the notification/CRM step.
- Once committed, this is frozen for Phase 2 — same rule as v1.

---

## Phase 2 — Parallel Tracks (3 people, fully independent once Phase 1 is committed)

### Track A — Drive Trigger, Triage, and Flagging

**Owner:** Person 1
**Files:** new `app/adapters/drive_store.py`, new `app/triage.py`, `app/adapters/pdf_reader.py` (reused, not modified unless the Railway contract changed)

**What to implement:**
1. Create the four Drive folders if absent (`create_file` with `mimeType: application/vnd.google-apps.folder`): `Inbox`, `Relevant`, `Review`, `Not-Relevant`. Record their folder IDs (env vars: `DRIVE_INBOX_ID`, `DRIVE_RELEVANT_ID`, `DRIVE_REVIEW_ID`, `DRIVE_NOT_RELEVANT_ID`).
2. `app/adapters/drive_store.py`: a small adapter wrapping the Google Drive MCP tools —
   - `list_inbox() -> list[file]` via `search_files(query="parentId = '<inbox_id>'")`
   - `download(file_id) -> bytes` via `download_file_content`
   - `move(file_id, dest_folder_id)` via `update_file(fileId=file_id, parentId=dest_folder_id)` — confirmed this replaces the existing parent (i.e., moves, not copies).
3. `app/triage.py`: a cheap, fast `TriageAgent` — **one** Claude call (no web search, no multi-step research), given the parsed deck text + the pipeline's fixed `FirmProfile.criteria`, that returns a `TriageResult` (`client.messages.parse(output_format=TriageResult)`). This is deliberately cheaper/faster than Track B's deep-dive — don't reuse the expensive research pipeline here.
4. The poller (can live in `app/pipeline.py` alongside Track A's code, or Track A can stub the deep-dive call as a TODO for Phase 3 to wire in):
   - Poll `Inbox/` on an interval (e.g. every 30–60s via a simple `while True: ...; time.sleep(N)` loop, or a Railway cron job).
   - For each new file: download → parse via existing `PdfReaderClient` → triage → `move()` to the matching folder.
   - No dedup database needed — a single sequential poller naturally can't double-process a file, since a moved file no longer matches the `Inbox/` query on the next poll.

**Docs to cite:** Phase 0 above (Drive tool facts); `docs/BUILD_PLAN.md` §"Step 1" (PDF reader contract, unchanged); `app/adapters/pdf_reader.py`.

**Verification:**
- Drop a real pitch-deck PDF into the `Inbox/` folder manually; confirm the poller picks it up, calls the Railway PDF reader successfully, and the file ends up in exactly one of `Relevant/`, `Review/`, `Not-Relevant/` within one poll interval.
- Confirm a `not_relevant` file does NOT trigger any downstream call (log this explicitly so it's demoable).
- Confirm re-running the poller after a file has moved does not re-process it.

**Anti-pattern guards:** don't build real Gmail push/pull for this hackathon (Phase 0 already ruled this out); don't run the expensive web-search deep-dive inside the triage call; don't invent a `move`/`watch` Drive tool that doesn't exist — `update_file`'s `parentId` is the only move primitive available.

---

### Track B — Deep-Dive Analysis Agent

**Owner:** Person 2
**Files:** new `app/diligence.py`, `app/config.py`, `requirements.txt`

This is almost entirely the same work as v1's Track B — if that was already built, extend it; if not, build it fresh. The only new requirement is populating `company` and `founders` (Phase 1's new fields) using web-search-grounded facts, since Slack/Attio need them.

**What to implement:**
1. `pip install anthropic`; add to `requirements.txt`; set `app/config.py` `anthropic_model` default to `"claude-sonnet-5"`.
2. `ClaudeDiligenceAgent.analyze(deck: ParsedDeck, firm: FirmProfile | None) -> DiligenceReport` in `app/diligence.py`, matching the `DiligenceAgent` Protocol in `app/agent.py`.
3. Two-call pattern:
   - **Research call:** `client.messages.create(..., tools=[{"type": "web_search_20260209", "name": "web_search"}])`, system prompt scoped to BUILD_PLAN.md §6 (TAM, competitors, founder) **plus** finding the company's website URL and each founder's LinkedIn URL and a one-line bio — these three are new requirements driven by the Slack notification spec.
   - **Structured extraction call:** `client.messages.parse(model=..., messages=[...], output_format=DiligenceReport)` → `response.parsed_output`.
4. **Pitfall:** `client.messages.parse` runs full pydantic validation client-side, including `Source`'s custom cross-field validator — wrap in try/except and retry once with a corrective message if validation fails (e.g. an external source missing a URL).
5. Load the firm profile once at pipeline startup from `PIPELINE_FIRM_PROFILE` env var (default `generic_seed`) via the existing `load_firm()` — no per-request firm selection anymore.
6. If a founder's LinkedIn URL or the company website can't be found via web search, leave the field `None` rather than guessing — Track C's Slack message must handle missing links gracefully (see Track C).
7. Develop against `tests/fixtures/sample_deck.json` from Phase 1 — don't block on Track A's live Drive polling.

**Docs to cite:** `docs/data-extraction.md` (qualitative reading, per Phase 1); `docs/BUILD_PLAN.md` §5, §6, §8; `app/agent.py`; `app/models.py`.

**Verification:**
- `ClaudeDiligenceAgent.analyze()` against the fixture deck produces a valid `DiligenceReport` with ≤5 findings, ≤5 questions, every external `Source` carrying a real-looking URL.
- `company.one_liner`, `company.website_url` (or `None`), and at least one `FounderSummary` are populated for a real sample deck with a findable founder LinkedIn presence.
- Generic (no firm profile edge case aside — firm is now always loaded from env, so just confirm the pipeline still runs if `PIPELINE_FIRM_PROFILE` is unset and `load_firm(None)` returns `None`).

**Anti-pattern guards:** same as v1 — no multi-agent orchestration, no vector DB, no hand-parsed JSON, no skipped evidence-rule retry, no assumed `web_search_result` field names.

---

### Track C — Attio + Slack Delivery

**Owner:** Person 3
**Files:** new `app/adapters/attio_client.py`, new `app/adapters/slack_notifier.py`

**What to implement:**
1. **Attio:** before writing any code, fetch `https://developers.attio.com`'s current API reference for creating/updating a company (and person) record and authenticating with an API key — do not guess endpoint paths or field names. Implement `save_to_attio(report: DiligenceReport) -> None` (or return the created record id) in `app/adapters/attio_client.py`, mapping `report.company` (and `report.founders`, if Attio's schema supports a linked person/founder object) onto whatever Attio's verified object schema requires.
2. **Slack:** for demo speed, use an **Incoming Webhook** (one URL, no bot token/scopes) unless the team already has a Slack app with a bot token set up — check with the team first. Implement `send_slack_notification(report: DiligenceReport) -> None` in `app/adapters/slack_notifier.py`, posting exactly the four required fields:
   - Company name (`report.company.name`)
   - One-liner (`report.company.one_liner`)
   - Founder bio one-liner + LinkedIn link, per founder in `report.founders` (if a `linkedin_url` is `None`, show the bio without a link rather than a broken/empty link)
   - Website link (`report.company.website_url`, omit the line if `None`)
   Use the `slack:block-kit` skill for a clean formatted message (header block for company name, section blocks for the rest) rather than a single unformatted text blob.
3. Build and test both adapters entirely against `tests/fixtures/sample_report.json` from Phase 1 — never blocked on Track A or B.
4. Both functions should be called only for `relevant`/`review` decks (Phase 0's triage-gate decision) — that gating logic lives in the Phase 3 pipeline wiring, not inside these adapters; keep these two functions triage-agnostic (just "given a report, deliver it").

**Docs to cite:** Phase 0 above (why there's no MCP shortcut here); `slack:block-kit`, `slack:slack-messaging`, `slack:slack-api` skills (load at build time for exact payload shapes); Attio's live developer docs (fetch at build time, don't rely on any cached knowledge of Attio's API).

**Verification:**
- Posting `tests/fixtures/sample_report.json` through `send_slack_notification` produces a real message in the team's Slack channel with all 4 fields correctly rendered, including the graceful-missing-link case (test with a fixture variant that has `website_url: null`).
- `save_to_attio` against the same fixture produces a real, inspectable record in the team's Attio workspace.
- Both functions raise/log clearly (not silently swallow) on an auth or schema error — this is the last step before the demo's payoff moment, so failures must be loud during development.

**Anti-pattern guards:** don't build a bespoke Slack bot with interactive components — a webhook post is enough for a notification; don't invent Attio field names before checking the docs; no numeric score anywhere in the Slack message or Attio record (Phase 1's resolved decision still applies to the output surface too).

---

## Phase 3 — Synthesis (whole team, short session)

1. Create `app/pipeline.py` that ties the three tracks together:
   ```text
   for file in drive_store.list_inbox():
       pdf_bytes = drive_store.download(file.id)
       deck = pdf_reader.parse(pdf_bytes, file.name)           # Track A (reused adapter)
       triage = triage_agent.classify(deck, firm)              # Track A
       drive_store.move(file.id, folder_for(triage.flag))      # Track A
       if triage.flag == "not_relevant":
           continue
       report = diligence_agent.analyze(deck, firm)            # Track B
       attio_client.save_to_attio(report)                      # Track C
       slack_notifier.send_slack_notification(report)          # Track C
   ```
2. Wire `PIPELINE_FIRM_PROFILE`, `DRIVE_*_ID`, Attio API key, and Slack webhook URL into `app/config.py`/env.
3. Decide how the poller runs for the demo: a simple long-lived process (`python -m app.pipeline`) is enough — no need for a Railway cron job unless the team wants it always-on beyond the demo.
4. End-to-end demo run: drop a real pitch deck into `Inbox/`, watch it get parsed, triaged, moved, deep-analyzed, and confirm both a new Attio record and a Slack message appear.
5. Update `README.md` to describe the new pipeline flow (it currently describes the old upload+dashboard flow).

**Verification checklist (final gate before demo):**
- `pytest` passes.
- One full real-deck run works end-to-end, watched live by the group, before the demo slot.
- A `not_relevant` deck is demoed too, to show the gate actually stops the pipeline (no Attio record, no Slack message, file correctly filed).
- No numeric composite score, weighting formula, auth system, or extra agents beyond triage + deep-dive snuck in during merge.

**Anti-pattern guard:** if Attio's or Slack's real API shape didn't match what Track C assumed while building against the fixture, fix it during this phase — don't ship a Track C that only ever ran against its own fixture.

---

## Optional — Demo Framing

`docs/judges_profiles.md` profiles the likely TechBBQ 2026 judges (Kellezi/byFounders, Milo, den Teuling). Not a build task, but worth 5 minutes before the demo: Kellezi responds to technical depth and authenticity over polish, den Teuling to provable ROI over hype — frame the walkthrough (not the product itself) accordingly. The "drop a deck in Drive, get a Slack ping with an Attio record already created" moment is the demo's payoff — sequence the walkthrough so that's the visible climax.
