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

(`TamSamSomBreakdown`, `CompetitorAnalysis`, `FounderProfile`, `MetricResult`, `Finding.pillar` are unchanged from v1. The full, current, consolidated class bodies — this is the single source of truth — are inlined in each of the three track files below, since each is meant to be opened standalone in its own session.)

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

Each track now has its own fully standalone plan file — open the linked file directly in that person's session; it restates the project context, the frozen contract, and doesn't require this master file to be open alongside it.

- **[Track A — Drive Trigger, Triage & Flagging](track-a-drive-trigger.md)** (Owner: Person 1) — watches the Drive `Inbox/` folder, runs a cheap triage classification, moves the file into `Relevant/`/`Review/`/`Not-Relevant/`. `app/adapters/drive_store.py`, `app/triage.py`.
- **[Track B — Deep-Dive Analysis Agent](track-b-deep-analysis.md)** (Owner: Person 2) — the Claude research agent: TAM/competitors/founder profile against pre-selected firm criteria, plus company/founder summary fields for delivery. `app/diligence.py`.
- **[Track C — Attio + Slack Delivery](track-c-delivery.md)** (Owner: Person 3) — pushes a finished report to Attio and posts the 4-field Slack notification. `app/adapters/attio_client.py`, `app/adapters/slack_notifier.py`.

All three build and verify against fixtures (`tests/fixtures/sample_deck.json`, `tests/fixtures/sample_report.json`) independently — none of them block on each other until Phase 3.

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
