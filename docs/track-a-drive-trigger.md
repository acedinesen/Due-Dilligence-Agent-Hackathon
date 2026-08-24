# Track A — Drive Trigger, Triage & Flagging

> **You are one of 3 parallel workstreams** for the KeepItSimple due diligence agent, built for the TechBBQ 2026 hackathon (Aug 26–27). This file is self-contained — you shouldn't need to open anything else to start working — but the full picture lives in `docs/PARALLEL_BUILD_PLAN.md` if you want it. The other two tracks are:
> - **Track B** (`docs/track-b-deep-analysis.md`) — the Claude agent that does the actual TAM/competitors/founder due diligence once a deck is flagged relevant/review. You do not build this; you call it (or, until it exists, stub it).
> - **Track C** (`docs/track-c-delivery.md`) — pushes Track B's output to Attio and Slack. You do not touch this at all.
>
> You can build and fully test your track in isolation, end-to-end, without either of the others existing yet.

---

## Project context

The product is a background pipeline, not a UI. A pitch deck lands in a watched Google Drive folder (standing in for "a pitch-deck email arrived" — see why below), gets triaged, filed into a folder that reflects the triage verdict, and — only if it passed triage — gets deep-analyzed and pushed to Attio + Slack.

```text
New file in Drive "Inbox/"                    ← THIS IS YOUR TRACK, starts here
        ↓
Parse PDF (existing Railway PDF reader adapter — reuse, don't rebuild)
        ↓
Triage agent: relevant / review / not_relevant   ← YOU BUILD THIS
        ↓
Move file to Relevant/ Review/ or Not-Relevant/  ← YOU BUILD THIS (the "flag")
        ↓                                          ← YOUR TRACK ENDS HERE
   (not_relevant → STOP, nothing downstream happens)
        ↓ relevant or review
Deep-dive agent: TAM / competitors / founder profile   ← Track B, not you
        ↓
Save to Attio + Slack notification                     ← Track C, not you
```

**Why Drive and not real Gmail:** this session's Gmail integration only exposes an auth handshake, no read/send/label tools — and building real Gmail push notifications (Cloud Pub/Sub, domain verification) isn't a two-day hackathon task. So for the demo, dropping a PDF into a Drive `Inbox/` folder simulates "a pitch-deck email was received." Wiring a real Gmail-to-Drive forwarder is future work, not part of this build.

**Why the triage gate matters:** `not_relevant` decks stop here — no deep analysis, no Attio record, no Slack noise. `relevant` and `review` both continue downstream. This mirrors how a VC associate actually triages inbound decks, and it's also why your triage call must be cheap/fast — it runs on every single inbound deck, unlike Track B's expensive research call which only runs on the ones that pass.

---

## What you're building

1. A small Google Drive adapter (list/download/move files).
2. A cheap, fast triage agent (one Claude call, no web research) that reads a deck and returns relevant/review/not_relevant + a reason.
3. A polling loop that ties these together: watch `Inbox/`, parse, triage, move.

You are the very first stage of the pipeline. Nothing upstream of you exists — you're triggered by a human dropping a file into Drive (for this hackathon; a real Gmail forwarder is out of scope).

---

## Verified facts you need (don't re-derive these — they were checked live against the actual tool schemas this session)

**Google Drive MCP tools** (`mcp__claude_ai_Google_Drive__*`) — pull their full schemas via `ToolSearch("select:mcp__claude_ai_Google_Drive__update_file,mcp__claude_ai_Google_Drive__create_file,mcp__claude_ai_Google_Drive__search_files,mcp__claude_ai_Google_Drive__download_file_content,mcp__claude_ai_Google_Drive__get_file_metadata")` before writing code — but the key facts are:

- **Moving a file = `update_file(fileId, parentId)`.** Setting `parentId` to a destination folder's id *replaces* the file's existing parent. This is the entire "flag into relevant/review/not-relevant" mechanic — there is no separate move/copy tool.
- **`search_files(query)`** takes a structured query language. Use `parentId = '<inbox_folder_id>'` to list what's currently in the Inbox. It also supports `modifiedTime` comparisons if you need them, and `title`/`fullText`/`mimeType` filters. Do not put file-type words like "pdf" or "deck" inside a `title contains` clause — map them to a `mimeType` clause instead (see the tool's own description for the exact syntax).
- **`create_file(title, parentId?, mimeType?)`** — pass `mimeType: "application/vnd.google-apps.folder"` to create a folder. Use this once to set up `Inbox`, `Relevant`, `Review`, `Not-Relevant` if they don't already exist, then hardcode/env-var their returned folder IDs.
- **`download_file_content`** and **`get_file_metadata`** exist on the same MCP server for fetching bytes and filenames — pull their schemas via ToolSearch when you get to that step; they weren't re-verified in detail this session, only confirmed to exist.
- There is **no** "watch for changes" tool. You must poll `search_files` on an interval.

**No dedup database needed.** Because you move a file the instant you claim it, a single sequential polling loop (no concurrency) can't double-process a file — once moved, it no longer matches the `Inbox/` query on the next poll. Don't build a "processed IDs" table; it's unnecessary complexity for this scale.

---

## Frozen data contract

This schema is shared across all 3 tracks and was frozen by the team — **do not rename or restructure it without a 2-minute sync with whoever owns Track B** (they're the other consumer of `FirmProfile`, and they own `DiligenceReport` which your triage step doesn't touch but should be aware of). The parts relevant to you:

```python
# app/models.py — already exists, reuse as-is
class DeckPage(BaseModel):
    page: int
    text: str


class ParsedDeck(BaseModel):
    filename: str
    full_text: str
    pages: list[DeckPage] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class FirmProfile(BaseModel):
    id: str
    name: str
    criteria: list[str] = Field(default_factory=list)
```

```python
# app/models.py — NEW, you own this one. Add it if it's not there yet.
class TriageResult(BaseModel):
    flag: Literal["relevant", "review", "not_relevant"]
    reason: str
```

If `TriageResult` doesn't exist in `app/models.py` yet, add it yourself as your first step — don't block waiting for someone else to do it.

---

## What to implement

**Files:** new `app/adapters/drive_store.py`, new `app/triage.py`, reuse `app/adapters/pdf_reader.py` unmodified (unless you discover the Railway PDF reader's contract has actually changed — see `docs/BUILD_PLAN.md` §"Step 1" for how it was originally verified).

1. **Create the four Drive folders** (once, manually or via a short script using `create_file`): `Inbox`, `Relevant`, `Review`, `Not-Relevant`. Store their IDs as env vars: `DRIVE_INBOX_ID`, `DRIVE_RELEVANT_ID`, `DRIVE_REVIEW_ID`, `DRIVE_NOT_RELEVANT_ID`.

2. **`app/adapters/drive_store.py`** — a small adapter, mirroring the style of the existing `app/adapters/pdf_reader.py` (one clear responsibility, isolated so the Drive mechanics can change without touching the rest of the app):
   ```python
   class DriveStore:
       def list_inbox(self) -> list[dict]: ...      # wraps search_files(parentId = inbox_id)
       def download(self, file_id: str) -> bytes: ...  # wraps download_file_content
       def move(self, file_id: str, dest_folder_id: str) -> None: ...  # wraps update_file(fileId, parentId=dest_folder_id)
   ```

3. **`app/triage.py`** — a `TriageAgent` with one method, e.g. `classify(deck: ParsedDeck, firm: FirmProfile | None) -> TriageResult`. Implement with **one** `client.messages.parse(model="claude-sonnet-5", messages=[...], output_format=TriageResult)` call — no web search, no multi-turn loop. Give it the deck's `full_text` and, if present, `firm.criteria`, and ask it to judge fit + basic credibility, not to do real research. This must be cheap and fast: it runs on every inbound deck, including the ones that get rejected.

4. **The poller** (`app/pipeline.py`, or your own module if Phase 3 hasn't created `pipeline.py` yet — whoever gets there first can create it): a simple loop —
   ```python
   for file in drive_store.list_inbox():
       pdf_bytes = drive_store.download(file["id"])
       deck = pdf_reader.parse(pdf_bytes, file["title"])   # existing adapter, unchanged
       triage = triage_agent.classify(deck, firm)
       drive_store.move(file["id"], folder_id_for(triage.flag))
       if triage.flag == "not_relevant":
           continue
       # hand off to Track B here — stub this call if Track B isn't built yet:
       # report = diligence_agent.analyze(deck, firm)
   ```
   Poll on an interval (30–60s via `time.sleep`, or a Railway cron job if you prefer) — no need for anything fancier for a hackathon demo.

5. Load the fixed firm profile once at startup via the existing `load_firm()` (`app/firm_profiles.py`) using an env var, e.g. `PIPELINE_FIRM_PROFILE=generic_seed` (default). There's no per-request firm selection anymore — no human is choosing it per deck.

---

## Docs to cite

- `docs/BUILD_PLAN.md` §"Step 1" — the original Railway PDF reader verification (unchanged, still applies).
- `app/adapters/pdf_reader.py` — reuse `PdfReaderClient` exactly as-is; only touch `_normalize()` if you discover the Railway service's JSON shape has actually changed.
- `app/firm_profiles.py`, `firm_profiles/generic_seed.json` — existing, reuse `load_firm()`.

---

## Verification checklist

- Drop a real pitch-deck PDF into the `Inbox/` folder by hand. Within one poll interval, confirm: the file is gone from `Inbox/`, it's parsed successfully via the Railway reader, and it lands in exactly one of `Relevant/`, `Review/`, `Not-Relevant/`.
- Confirm a deck that gets `not_relevant` does **not** trigger any downstream call — log this explicitly (`print`/logging is fine) so it's visibly demoable.
- Confirm re-running the poller after a file has already moved does not re-process it (there should be nothing left in `Inbox/` to find).
- Sanity-check the triage call is fast (a few seconds, not tens of seconds) — if it's slow, you've probably accidentally given it web-search tools or an overly long prompt; that work belongs to Track B, not you.

## Anti-pattern guards

- Don't build real Gmail push/pull integration — already ruled out for this hackathon (see "Why Drive and not real Gmail" above).
- Don't give the triage agent web-search tools or make it do real due diligence — that's Track B's job, and doing it here defeats the point of a cheap pre-filter.
- Don't invent a Drive "move" or "watch" tool — `update_file`'s `parentId` is the only move primitive that exists; there is no watch/webhook tool, only polling.
- Don't build a processed-files database — the sequential poller + immediate move already prevents double-processing.
- Don't rename `ParsedDeck`/`DeckPage`/`FirmProfile` fields — they're shared with Track B.

## Handoff

You don't depend on Track B or C to build or test your track — you can verify the entire Inbox → triage → folder-move loop on your own with a real Drive account and a real PDF. When Track B exists, the only integration point is one function call (`diligence_agent.analyze(deck, firm)`) inserted where the stub comment is in step 4 above — that wiring happens in the whole-team synthesis session, not by you alone.
