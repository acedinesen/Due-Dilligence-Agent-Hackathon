# Gmail intake — setup guide

**What you get:** a founder emails a pitch deck, and within ~5 minutes the deck
is in Drive `Inbox/` and the existing pipeline has picked it up. Gmail becomes
the trigger.

```text
new mail with a PDF        scripts/gmail_intake.gs         app/pipeline.py
"Pitch Deck – Acme"  ──▶   (Apps Script, every 5 min)  ──▶  polls Inbox/ every 45s
                            labels the thread,               parse → triage → file
                            drops the PDF in Inbox/          → deep dive → Attio + Slack
```

**The Python side needed zero changes.** `Inbox/` is still the only work queue,
and `app/pipeline.py` still has no idea Gmail exists. Nothing in `app/` was
touched to build this.

Time to set up: about 10 minutes, all of it in a browser.

---

## 1. Why this is a standalone script and not a Gmail add-on

You asked for an add-on. An add-on cannot do this, and it is better to know that
now than at 3 a.m. on demo day:

- **There is no new-mail trigger for add-ons.** Gmail/Workspace add-ons support
  exactly four kinds of trigger: homepage, contextual (the user *opens* a
  message), compose, and preview-link. Every one of them requires a human to be
  clicking in the UI. The Workspace Events API, which does offer real push
  subscriptions, covers Chat, Drive and Meet — Gmail is not on the list.
- **30-second cap.** Add-on executions are killed at 30 seconds. Downloading
  several multi-megabyte decks does not reliably fit.
- **Once per hour.** Add-on *installable* triggers are capped at one run per
  hour. A pitch deck arriving at 14:01 might sit until 15:00.

A **standalone Apps Script with a time-driven trigger** has none of those caps:
minimum interval **1 minute**, 6 minutes of runtime per execution, and it runs
with no browser open and nobody logged in. That is the closest real thing to
"an add-on that fires on new mail", so that is what `scripts/gmail_intake.gs`
is.

It also removes a problem rather than adding one. An installable trigger **runs
as the account that created it**, so:

- no service account, no OAuth client, no GCP project, no domain-wide delegation;
- **no secrets anywhere in the script** — there are none to store;
- files it creates are **owned by and billed to a real user's Drive**, which is
  precisely what the service account could not do (it has no storage quota of
  its own and answers `403 storageQuotaExceeded`).

---

## 2. Before you start

You need:

1. **The Drive `Inbox/` folder id** — the `Inbox` value printed by
   `scripts/setup_drive_folders.py`, i.e. the same string as `DRIVE_INBOX_ID`
   in `.env`. If you no longer have it, open the folder in Drive and copy the
   last segment of the URL: `.../folders/`**`<this bit>`**.
2. **The Google account that receives the decks** — you must be signed in as it,
   because the trigger will run as it.
3. **That account must be able to write to the `Inbox/` folder**, and the
   pipeline's service account must still have Editor on the folder tree (it
   needs to move files out of `Inbox/` into `Relevant/`, `Review/` or
   `Not-Relevant/`). The normal setup already satisfies both: the human owns the
   folder tree and shared it with the service account as Editor.

---

## 3. Create the script

1. Go to **[script.google.com](https://script.google.com)** — check the account
   avatar top-right is the mailbox that receives the decks.
2. **New project**.
3. Delete the `function myFunction() {}` stub in `Code.gs` and paste the entire
   contents of **`scripts/gmail_intake.gs`**.
4. At the top of the pasted code, set:

   ```javascript
   var INBOX_FOLDER_ID = "PASTE_DRIVE_INBOX_ID_HERE";   // <-- your Inbox folder id
   ```

   If you forget, the script throws a clear error telling you so rather than
   failing quietly.
5. Rename the project something you will recognise in six months
   (*Untitled project* → **Pitch deck intake**), then **save** (the disk icon,
   or `Cmd/Ctrl+S`).

Everything else at the top of the file — label name, search query, thread cap,
trigger interval — is a named constant with a comment explaining what it costs
you to change it. Leave the defaults for the demo.

---

## 4. Run it once by hand — this is what triggers consent

In the toolbar, make sure the function dropdown says **`processPitchDeckEmails`**
and click **Run**.

You will hit an authorisation flow. Expect it; it is friction, not a blocker:

1. **Review permissions** → choose the account.
2. **"Google hasn't verified this app"** →
   **Advanced** → **Go to *Pitch deck intake* (unsafe)**.
3. **Allow**.

> **Why the scary screen, and why it is fine.** Verification applies to apps
> *published to other people*. This is your own script, in your own account,
> written by you, running as you — there is no third party in the picture. Every
> Apps Script anyone writes shows this screen. You cannot make it go away
> without submitting the project for OAuth verification, which would be
> pointless here.

The first run does the work immediately: it creates the `PitchDeckProcessed`
label and processes anything already matching. Open **Execution log** (bottom
panel) and confirm you see a `Run start:` line.

### What it asks for, and why

| Scope | Why the script needs it |
|---|---|
| `https://mail.google.com/` (Gmail, read/modify) | `GmailApp.search` to find the mail, `getAttachments` to read the PDF, `addLabel` to mark the thread done. It never sends or deletes mail. |
| `https://www.googleapis.com/auth/drive` | `Folder.createFile` needs the broad Drive scope; the narrower `drive.file` scope cannot write into a pre-existing folder it did not create. |
| `https://www.googleapis.com/auth/script.scriptapp` | Only if you use `setUpTrigger()` — managing the script's own triggers. |

**Apps Script works out which scopes to request by scanning the source, and it
does not skip commented-out code.** A single dead `MailApp.sendEmail(...)` line
in a comment would add a send-mail scope to that consent screen. If you add or
delete code and the consent screen changes, that is why.

---

## 5. Install the trigger

**Recommended route — the UI**, because it is the only way to set failure
notifications, and you want those: a background trigger's exceptions surface
nowhere. No email, no banner, nothing but a line in a log nobody is reading.

1. Left sidebar → **Triggers** (the alarm-clock icon) → **Add Trigger**.
2. Set exactly:
   - Choose which function to run: **`processPitchDeckEmails`**
   - Which runs at deployment: **Head**
   - Select event source: **Time-driven**
   - Select type of time based trigger: **Minutes timer**
   - Select minute interval: **Every 5 minutes**
   - Failure notification settings: **Notify me immediately**
3. **Save**.

**Fast route — from the editor.** Select **`setUpTrigger`** in the function
dropdown and **Run** once. It installs the same 5-minute trigger and deletes any
earlier trigger for the same handler, so running it twice cannot leave you with
two. It **cannot** set the notification preference — if you use this route, still
open **Triggers** afterwards and switch the new trigger to *Notify me
immediately*.

### Choosing the interval

`everyMinutes()` accepts **only 1, 5, 10, 15 or 30** — anything else throws.

- **5 minutes (default).** 288 runs/day. On Workspace, the 6 h/day trigger
  runtime budget gives each run ~75 seconds. Comfortable.
- **1 minute.** Makes a live demo feel instant. On Workspace it is fine. On a
  **consumer** `@gmail.com` account the trigger budget is only **90 min/day**,
  so 1440 runs/day leaves ~3.75 s per run — one slow deck and you start
  exhausting the daily quota. Use 5 on a consumer account.

Other ceilings, none of which a demo will reach: 6 minutes per execution, 50,000
Gmail reads/day (Workspace), 30 simultaneous executions per user.

---

## 6. Verify end to end (4 steps)

1. **Send a test mail** to the account with subject `Pitch Deck – TestCo` and a
   PDF attached. The subject must contain the words *Pitch Deck* — the query is
   `has:attachment subject:"Pitch Deck" -label:PitchDeckProcessed newer_than:2d`.
   Send it from a *different* account if you can; mail to yourself works too.
2. **Check Executions** (sidebar, the list icon). Within one interval a
   `processPitchDeckEmails` row appears with status *Completed*. Expand it and
   you should see a `SAVED message=… -> driveFile=…` line.
3. **Check the Gmail thread.** It now carries the **`PitchDeckProcessed`** label.
4. **Check Drive `Inbox/`.** A file named `<gmailMessageId>__<attachmentName>`
   is there, e.g. `18f2a9c4b1__TestCo Deck.pdf`. That prefix is deliberate: it
   traces any file in Drive back to the exact email it came from.
5. **Wait one more interval and check that no second copy appears.** This is the
   step people skip and the one that matters — it proves the idempotency is real
   and you are not about to pay for the same deck every 5 minutes.

If the pipeline is running, `app/pipeline.py` picks the file up out of `Inbox/`
within 45 seconds, triages it, moves it to `Relevant/`, `Review/` or
`Not-Relevant/`, and — unless it was flagged `not_relevant` — runs the deep dive
and delivers to Attio and Slack. From `Inbox/` onward, nothing about this is new.

---

## 7. How it avoids processing a deck twice

Reprocessing is the expensive failure: each deep dive costs roughly $1 and ~5
minutes, and a duplicate would recur on *every* trigger interval until someone
noticed. So there are three independent layers, and the script deliberately
prefers dropping a deck over duplicating one.

1. **`-label:PitchDeckProcessed` in the query.** Server-side exclusion.
2. **An in-code label re-check.** Not belt-and-braces theatre: Google's own
   documentation for search operators warns that with a negative operator
   *"conversations with excluded criteria may still appear"*, because Gmail
   matches **messages** first and **conversations** second. A thread whose newest
   message is not itself labelled can come back from a `-label:` query anyway.
   Without the re-check, such a thread would be reprocessed on every run.
3. **Deterministic filenames.** `<gmailMessageId>__<attachmentName>` plus an
   existence check in `Inbox/`. Even if both layers above failed, the same
   attachment maps to the same filename and the write is skipped.

On top of that:

- **The thread is labelled *before* the file is written**, never after. The two
  failure modes are not symmetric. Crash after labelling → the deck is dropped:
  costs $0, is visible in Executions, and recovery is deleting one label. Crash
  after writing but before labelling → the deck is processed **twice, silently,
  forever**. This is the same trade `app/pipeline.py` already makes when it files
  a deck into its destination folder *before* running the deep dive.
- **A script lock (`tryLock(0)`)** means an overlapping run exits instantly
  instead of racing the one already in flight.
- **`newer_than:2d`** bounds the scan, so cost does not grow with the mailbox.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Executions is empty | The trigger was never saved. Sidebar → **Triggers** → confirm a row exists. |
| `INBOX_FOLDER_ID is still the placeholder` | Set the constant at the top of the file, save, run again. |
| Nothing matches your test mail | The subject must contain *Pitch Deck*, and the mail must have an attachment. Try the query verbatim in the Gmail search box: `has:attachment subject:"Pitch Deck" -label:PitchDeckProcessed newer_than:2d`. |
| Thread labelled, no file in Drive | The save failed after the commit point — by design, so it fails loudly instead of duplicating. The Executions log has the reason (`will NOT be retried`). Usual causes: wrong folder id, or the account cannot write to that folder. To retry: remove the `PitchDeckProcessed` label from the thread. |
| `NO PDF thread …` in the log | The mail matched but carried no PDF (a `.docx`, or only inline signature images). It is labelled anyway so it stops matching. |
| Pipeline never picks the file up | That is the Drive side, not this script. Check `DRIVE_INBOX_ID` in `.env` is the *same* folder this script writes to, and that the service account still has Editor on the folder tree. |
| Deck arrived twice | Should be impossible via this script. Check you do not have **two** triggers installed (Triggers list) *and* re-read section 7 — but note two triggers still cannot duplicate a file; the lock and the filename check hold. Far likelier: the deck was also uploaded to `Inbox/` by hand. |
| Want to reprocess a deck | Remove the `PitchDeckProcessed` label from the thread **and** delete the `<messageId>__…` file from `Inbox/`. Both layers have to be cleared. |

---

## 9. What was and was not changed

- **New:** `scripts/gmail_intake.gs`, this document.
- **Unchanged:** everything in `app/`, all tests, `.env` / `.env.example`, the
  Drive folder layout, the Supabase schema. No new Python dependency, no new
  environment variable, no new credential.

The intake writes into the same queue a human drag-and-drop writes into, so both
paths keep working. You can still drop a PDF into `Inbox/` by hand and the
pipeline will not know the difference.

## 10. Two things verified live, and one warning

**The service account CAN pick up what this script creates — confirmed, not assumed.**
This was the one genuine risk in the design: the script runs as *you*, so the
files it creates in `Inbox/` are owned by *you*, while the pipeline authenticates
as the service account. Tested end to end against the real Drive on 2026-08-24: a
user-owned PDF placed in `Inbox/` was listed, downloaded (bytes matched by
sha256) and reparented to `Review/` by the service account, ending with exactly
one parent and no longer in `Inbox/`. Ownership stayed with the user.

The reason it works is worth knowing, because it decides whether it works for
your teammates too: **the four folders are themselves owned by the service
account**, so every file created inside them inherits the service account as a
`writer`. So this works for any account that drops a deck in, not only the one
running the script.

One asymmetry that follows from the same fact: the service account **cannot trash
or delete** a user-owned file (`403 insufficientFilePermissions`). Irrelevant
today, since the pipeline only ever moves files — but any future "delete after
processing" step will fail on exactly the Gmail-sourced decks and nothing else.

**WARNING — do not commit your real `INBOX_FOLDER_ID` to git while the folders
are shared `anyone: writer`.** As of this writing all four pipeline folders carry
a public link-writer permission. A folder id is not normally a secret, but
combined with `anyone: writer` it *is* write access: publishing the id in a
repository effectively publishes the ability to drop files into `Inbox/`, and
every file dropped there costs roughly a dollar and five minutes of analysis.
`INBOX_FOLDER_ID` is therefore left as a placeholder in
`scripts/gmail_intake.gs` on purpose — paste your id into the Apps Script
editor, not into the committed file. Better still, tighten the folder sharing
first: replace `anyone: writer` with explicit per-person access.
