# Leftover plan — what's still open after Tracks A, B, C

Generated 2026-08-24 after finalizing Track C and auditing Tracks A/B against
their own verification checklists. Every item below has a specific piece of
evidence behind its status — this is not a guess pass. "Passing unit test
with a mocked client" does **not** count as "done" here; only a real run
against the real external service does.

Status legend: **BLOCKED-ON-HUMAN** (needs a browser/credential action only
the account owner can do), **UNTESTED-LIVE** (code looks right, only proven
by mocked tests), **DOC-DRIFT** (docs no longer match the shipped code).

---

## 1. Credentials — block the demo today

### 1.1 `ATTIO_API_KEY` is dead — BLOCKED-ON-HUMAN
Live-tested this session: a direct `httpx.get` to `api.attio.com/v2/objects`
with the key currently in `.env` returns `401 unauthorized —
"The API Key provided could not be found... token having been revoked"`,
straight from Attio's server, not from our code. `save_to_attio()`'s request
shape itself was independently re-verified against `developers.attio.com`'s
live docs and is correct.

**To close:** get a fresh key — Attio workspace → Settings → Developers →
Access tokens — drop it into `.env`'s `ATTIO_API_KEY`, then re-run:
```
python3 -c "
import asyncio, json
from dotenv import load_dotenv; load_dotenv()
from app.adapters.attio_client import save_to_attio
from app.models import DiligenceReport
report = DiligenceReport.model_validate(json.load(open('tests/fixtures/sample_report_urls_populated.json')))
print(asyncio.run(save_to_attio(report)))
"
```
A returned URL/id closes this out. Until then, Track C's Attio delivery is
unproven against the real workspace.

### 1.2 No Slack credentials configured at all — BLOCKED-ON-HUMAN
`.env` has none of `SLACK_WEBHOOK_URL`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`.
`send_slack_notification()` was confirmed to fail loudly and correctly on
missing config (no silent swallow) — that's the one thing verifiable without
real Slack access. The payload itself is confirmed well-formed (passed
Slack's own `blocks.validate` API against the real fixture), including the
`<url|label>` link syntax that a past session flagged as a risk — it's
correct, not a regression.

**To close:** create one Incoming Webhook (Slack app config → Incoming
Webhooks → Add New Webhook to Workspace) and put the URL in
`SLACK_WEBHOOK_URL`, then run `send_slack_notification()` against
`tests/fixtures/sample_report_urls_populated.json` and confirm a real message
lands in the channel.

---

## 2. Never run end-to-end for real — UNTESTED-LIVE

### 2.1 Full pipeline: Drive Inbox → triage → move → deep-dive → deliver
No test exercises `app/pipeline.py`'s `run_once`/`poll_forever` as a whole,
and nothing in git history or code comments claims a real PDF has gone all
the way through. `docs/PARALLEL_BUILD_PLAN.md` calls this the final demo
gate ("one full real-deck run works end-to-end, watched live by the group")
— it hasn't happened yet.

**To close before the demo:** drop one real pitch-deck PDF into the live
`Inbox/` folder and watch it move to `Relevant/`/`Review/`/`Not-Relevant/`,
then (if not `not_relevant`) confirm a real Attio record + Slack message
follow. This single manual run also closes out items 2.2–2.4 below as a
side effect.

### 2.2 Drive mechanics were only proven for one edge case
The one live Drive test on record (`docs/gmail-intake-setup.md` §10) proves a
user-owned file can be listed/downloaded/reparented by the service account —
it never went through triage or parsing. `list_inbox`/`download`/`move`
themselves are solid; the gap is that nothing has chained them together with
a real PDF.

### 2.3 `not_relevant` gate and no-reprocessing behavior
Both are correct by code inspection (`app/pipeline.py:78-85` logs
`SKIPPED downstream`; a moved file no longer matches `list_inbox`'s query) but
neither has a test or a live run backing it. Low risk, cheap to fix — see §4.

### 2.4 Anthropic triage fallback (`TRIAGE_PROVIDER=anthropic`) never actually called live
Only exercised via a mocked SDK client in `tests/test_triage.py`. The prior
attempt at this path (commit `770a6ab`) noted its own key returned 401 and
verification was never completed. If this fallback is ever needed, test it
live once before relying on it — don't assume it works just because the
OpenRouter path does.

### 2.5 Track B's "found via search" branch has never actually run
`tests/fixtures/sample_report_urls_populated.json` — the fixture with a
populated `website_url` and founder `linkedin_url`s — is **hand-written**
(confirmed by commit `3f6f2da`'s own message), not a real agent output. It
doesn't even describe the same company as `tests/fixtures/sample_deck_findable.json`
(the deck fixture says "Performativ", the report fixture says "Verdiq" with
different founders) — the two were never actually connected by a real run.
`sample_report.json` (the *other* fixture) **is** a confirmed-real output,
but only for the "company not findable" path.

**To close:** run `ClaudeDiligenceAgent.analyze()` for real against
`sample_deck_findable.json` and save the genuine output over
`sample_report_urls_populated.json`, the way `sample_report.json` was
produced. This proves the populated-URL path actually works end to end and
gives Track C a real (not fabricated) populated fixture to demo against.

### 2.6 `firm=None` vs a real `FirmProfile` — only one is confirmed live
Code branches correctly, but there's no record of which firm value the one
confirmed live run used, and no test pins the other branch.

---

## 3. No automated regression coverage — UNTESTED-LIVE

None of these are known-broken; they're just unprotected. A future edit could
break any of them silently.

- **`app/diligence.py`'s orchestration** (`_research`, `_extract`,
  `_collect_blocks`, `_unwrap_envelope`, the retry loop) — only the pure
  helpers (`_uncited_urls`, `_url_key`) are unit-tested. The two-call
  research/extraction loop itself has zero test coverage; it's validated only
  by the fact that it once produced `sample_report.json` for real.
- **`app/adapters/pdf_reader.py`** — no `test_pdf_reader.py` at all. The
  `/parse` → `/extract` route fix (commit `770a6ab`) was verified against the
  service's own `/openapi.json`, not by a passing test or a confirmed
  post-fix successful parse.
- **`app/pipeline.py`'s `run_once`** — see 2.1/2.3/2.4; no test drives it with
  a mocked `DriveStore`/`TriageAgent`/`ClaudeDiligenceAgent` to lock in the
  filing-before-deep-dive ordering, the per-stage failure isolation, or the
  independent Attio/Slack delivery try/excepts that `app/pipeline.py`'s own
  comments describe as load-bearing.

**To close (all three, cheaply, no live calls needed):** write mocked unit
tests the same way `tests/test_triage.py` and `tests/test_attio_client.py`
already do for their modules — this is ordinary engineering follow-up, not
blocked on anything external.

---

## 4. Doc/code drift — DOC-DRIFT

- **`docs/track-a-drive-trigger.md`** still mandates
  `client.messages.parse(...)` on Anthropic for triage. Shipped code defaults
  to a completely different mechanism (OpenRouter, `response_format:
  json_schema`, `reasoning: {"enabled": false}`). Update the doc to describe
  `settings.triage_provider` and both paths, or at minimum add a note pointing
  at `app/triage.py`'s own docstring.
- **`README.md`** still describes the old "VC uploads a deck to a
  Supabase-backed dashboard, mock agent" flow, not the actual
  Drive/Gmail-triggered pipeline. `docs/PARALLEL_BUILD_PLAN.md` lists
  "Update README.md to describe the new pipeline flow" as a to-do that was
  never done.
- **`app/adapters/supabase_store.py`** and the `SUPABASE_*` settings appear to
  be leftovers from that older flow — unused by the current Drive → triage →
  diligence → delivery pipeline. Worth confirming and deleting if genuinely
  dead, rather than carrying unused config forward.

---

## 5. Open, unverifiable from this repo — BLOCKED-ON-HUMAN

- **Drive folders currently carry `anyone: writer` public sharing**
  (`docs/gmail-intake-setup.md` §10's own warning, as of last writing). This
  is a real access-control issue on the live folders, not something checkable
  from the repo. Tighten to explicit per-person access before publishing
  anything with the folder id in it (the id is deliberately still a
  placeholder in `scripts/gmail_intake.gs` because of this).
- **The Gmail Apps Script has never been installed against a real Gmail
  account.** `scripts/gmail_intake.gs` ships with `INBOX_FOLDER_ID` as a
  placeholder on purpose. This is an interactive, ~10-minute browser task
  (`docs/gmail-intake-setup.md` §§3–5) that only the account owner can do —
  nothing to fix in code.

---

## Suggested order if closing all of this before the demo

1. Rotate `ATTIO_API_KEY` (§1.1) — 5 min, unblocks Track C's own checklist.
2. Set up a Slack webhook (§1.2) — 5 min, unblocks Track C's own checklist.
3. Run the Gmail Apps Script setup (§5) — ~10 min, one-time.
4. Drop one real deck in `Inbox/` and watch it go all the way through (§2.1)
   — this one run also closes 2.2–2.4 as a side effect and is the actual demo
   rehearsal, not just a verification checkbox.
5. Everything in §3 and §4 is safe to leave for after the demo — none of it
   is known-broken, it's just unprotected/undocumented.
