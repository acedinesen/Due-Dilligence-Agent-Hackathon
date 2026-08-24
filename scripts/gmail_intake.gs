/**
 * gmail_intake.gs — makes a new Gmail message the trigger for the pipeline.
 *
 * WHAT IT DOES
 *   Every few minutes it searches the mailbox for unprocessed "Pitch Deck" mail
 *   with attachments, labels each matching thread, and drops every PDF
 *   attachment into the Drive `Inbox/` folder that app/pipeline.py already
 *   polls. The Python side is unchanged and knows nothing about Gmail: `Inbox/`
 *   stays the one and only work queue.
 *
 *       new mail --> [this script] --> Drive Inbox/ --> app/pipeline.py
 *
 * WHY A STANDALONE SCRIPT AND NOT A GMAIL ADD-ON
 *   A Gmail/Workspace add-on cannot trigger on new mail at all. Add-ons support
 *   only homepage, contextual (the user opens a message), compose and
 *   preview-link triggers, and the Workspace Events API covers Chat, Drive and
 *   Meet — not Gmail. Add-on executions are also capped at 30 seconds and
 *   add-on installable triggers at once per hour. A standalone Apps Script on a
 *   time-driven trigger has neither cap, and is the closest real thing to "an
 *   add-on that fires on new mail". Full reasoning: docs/gmail-intake-setup.md.
 *
 * WHY THERE ARE NO CREDENTIALS IN HERE
 *   An installable trigger runs as the account that created it, so this script
 *   reads that account's Gmail and writes to that account's Drive under its own
 *   consent. No service account, no OAuth client, no GCP project, no
 *   domain-wide delegation — and NO SECRETS IN THIS FILE, because this design
 *   needs none. It also sidesteps the service account's storage problem: files
 *   created here are owned by and billed to a real user's Drive, so there is no
 *   403 storageQuotaExceeded.
 *
 * SETUP
 *   docs/gmail-intake-setup.md. Short version: paste this into a new project at
 *   script.google.com, set INBOX_FOLDER_ID below, run processPitchDeckEmails
 *   once by hand to clear the consent screen, then add a 5-minute time-driven
 *   trigger (or run setUpTrigger once).
 */

// ---------------------------------------------------------------------------
// CONFIG — the only lines you need to edit. No secrets belong here.
// ---------------------------------------------------------------------------

/**
 * The Drive folder the pipeline polls: the `Inbox` id printed by
 * scripts/setup_drive_folders.py — the same value as DRIVE_INBOX_ID in .env.
 * It is the folder ID, not the name; if you no longer have the script output,
 * open the folder and copy the last path segment of the URL (/folders/<THIS>).
 */
var INBOX_FOLDER_ID = "PASTE_DRIVE_INBOX_ID_HERE";

/**
 * Deliberately space-free. This name is interpolated into SEARCH_QUERY as
 * `-label:PitchDeckProcessed`, and there is no documented rule for how spaces
 * or case in a label name must be written inside a `label:` search operator.
 * A single-token name avoids the question entirely.
 */
var LABEL_NAME = "PitchDeckProcessed";

/**
 * Idempotency layer 1 of 3: `-label:` excludes already-processed threads
 * server-side. `newer_than:2d` bounds the scan so its cost does not grow with
 * the mailbox, and so a thread that somehow keeps failing stops being retried
 * after two days instead of forever.
 *
 * If you rename the label, LABEL_NAME above updates both places at once.
 */
var SEARCH_QUERY =
  'has:attachment subject:"Pitch Deck" -label:' + LABEL_NAME + " newer_than:2d";

/**
 * Explicit ceiling on threads per run. The GmailApp.search reference documents
 * no maximum for its `max` argument, so we cap it here instead of trusting an
 * unstated default: one run has to fit inside the 6-minute per-execution limit
 * and inside the daily trigger-runtime quota (6 h/day on Workspace, 90 min/day
 * on a consumer account).
 */
var MAX_THREADS = 20;

/**
 * Minutes between runs. everyMinutes() accepts ONLY 1, 5, 10, 15 or 30 — any
 * other value throws. 5 is the default: 288 runs/day, which leaves ~75 s of
 * runtime budget per run on Workspace. Set it to 1 if a demo needs to feel
 * instant, but note 1440 runs/day leaves only ~3.75 s per run of a consumer
 * account's 90-minute daily allowance.
 */
var TRIGGER_INTERVAL_MINUTES = 5;

/** The function the time-driven trigger calls. Keep in sync with the name below. */
var HANDLER_FUNCTION = "processPitchDeckEmails";

// ---------------------------------------------------------------------------
// ENTRY POINT — this is the function the trigger runs.
// ---------------------------------------------------------------------------

function processPitchDeckEmails() {
  // A 5-minute trigger can fire while the previous run is still working (up to
  // 30 executions per user are allowed to run at once), and two concurrent runs
  // would both see the same thread as unlabelled. tryLock(0) means "take the
  // lock or give up right now" — never wait, because waiting only burns the
  // runtime quota this execution was given. The loser exits immediately; the
  // next tick picks the work up anyway.
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(0)) {
    Logger.log("SKIPPED RUN: another execution still holds the script lock.");
    return;
  }

  try {
    runIntake_();
  } finally {
    // finally, not the end of the try body: an exception must not leave the lock
    // held until it expires, or every run until then is a silent no-op.
    lock.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// INTERNALS
// ---------------------------------------------------------------------------

function runIntake_() {
  if (!INBOX_FOLDER_ID || INBOX_FOLDER_ID.indexOf("PASTE_") === 0) {
    throw new Error(
      "INBOX_FOLDER_ID is still the placeholder. Set it at the top of this file " +
        "to the Drive Inbox folder id (the same value as DRIVE_INBOX_ID in .env)."
    );
  }

  var folder = DriveApp.getFolderById(INBOX_FOLDER_ID);
  var label = getOrCreateLabel_(LABEL_NAME);
  var threads = GmailApp.search(SEARCH_QUERY, 0, MAX_THREADS);

  Logger.log(
    "Run start: query [" +
      SEARCH_QUERY +
      "] matched " +
      threads.length +
      " thread(s) (cap " +
      MAX_THREADS +
      ")."
  );

  var savedTotal = 0;
  for (var i = 0; i < threads.length; i++) {
    var thread = threads[i];
    var threadId = "<unknown>";
    // Per-thread isolation, mirroring app/pipeline.py's per-file isolation: one
    // malformed mail must never cost us the rest of the batch.
    try {
      threadId = thread.getId();
      savedTotal += processThread_(thread, label, folder);
    } catch (err) {
      Logger.log("ERROR thread " + threadId + ": " + describeError_(err));
    }
  }

  Logger.log("Run done: " + savedTotal + " file(s) written to Drive Inbox/.");
}

function processThread_(thread, label, folder) {
  var threadId = thread.getId();
  var subject = thread.getFirstMessageSubject();

  // Idempotency layer 2 of 3 — and this is NOT decoration. Gmail's own search
  // help warns that when a negative operator is used, "conversations with
  // excluded criteria may still appear", because Gmail matches messages first
  // and conversations second: a thread whose newest message is not itself
  // labelled can come back from a `-label:` query even though the thread does
  // carry the label. Without this re-check such a thread is reprocessed on
  // every single run.
  if (threadHasLabel_(thread, LABEL_NAME)) {
    Logger.log(
      "SKIP thread " +
        threadId +
        " (" +
        subject +
        "): already labelled " +
        LABEL_NAME +
        " — the -label: query term did not exclude it."
    );
    return 0;
  }

  // === COMMIT POINT: label BEFORE writing anything to Drive. ===
  // The ordering is deliberate and the asymmetry is the whole point. It is the
  // same trade as "file the deck before the deep dive" in app/pipeline.py:
  //
  //   label first, crash before the save -> this deck is DROPPED. Costs $0,
  //       fails visibly (nothing appears in Inbox/, the Executions log carries
  //       the exception), and recovery is deleting one label from one thread.
  //
  //   save first, crash before the label -> this deck is processed TWICE, and
  //       SILENTLY: the pipeline picks the duplicate up, spends ~$1 and ~5 min
  //       on a second deep dive, files a second Drive copy and writes a second
  //       CRM record — and it repeats on every trigger interval until a human
  //       happens to notice.
  //
  // A dropped deck is the cheaper, louder, recoverable failure. Take it.
  thread.addLabel(label);

  var messages = thread.getMessages();
  var pdfCount = 0;
  var savedCount = 0;

  for (var i = 0; i < messages.length; i++) {
    var message = messages[i];
    var messageId = message.getId();

    var attachments;
    try {
      // includeInlineImages defaults to TRUE, which would hand us every logo in
      // every signature in the thread as an attachment to sift through.
      attachments = message.getAttachments({ includeInlineImages: false });
    } catch (err) {
      Logger.log(
        "ERROR reading attachments of message " + messageId + ": " + describeError_(err)
      );
      continue;
    }

    for (var j = 0; j < attachments.length; j++) {
      var attachment = attachments[j];
      if (!isPdf_(attachment)) {
        continue;
      }
      pdfCount++;
      // Per-attachment isolation: one message may legitimately carry several
      // PDFs (deck + financials), and one unreadable blob must not take its
      // siblings with it — the thread is already labelled, so nothing in here
      // gets a second attempt.
      try {
        if (saveAttachment_(message, messageId, attachment, j, folder, subject)) {
          savedCount++;
        }
      } catch (err) {
        Logger.log(
          "ERROR saving attachment " +
            j +
            " of message " +
            messageId +
            ": thread " +
            threadId +
            " is already labelled, so this attachment will NOT be retried — " +
            "re-send the mail or remove the label to retry. " +
            describeError_(err)
        );
      }
    }
  }

  if (pdfCount === 0) {
    // Labelled anyway, on purpose: an unlabelled non-PDF match keeps coming back
    // from the query on every run for the next two days.
    Logger.log(
      "NO PDF thread " +
        threadId +
        " (" +
        subject +
        "): matched the query but carries no PDF attachment. Labelled " +
        LABEL_NAME +
        " so it stops matching."
    );
  }

  return savedCount;
}

function saveAttachment_(message, messageId, attachment, index, folder, subject) {
  var filename = messageId + "__" + attachmentName_(attachment, index);

  // Idempotency layer 3 of 3: the filename is derived only from the Gmail
  // message id and the attachment name, so the same attachment always maps to
  // the same Drive filename and no re-run can produce a second copy. It doubles
  // as traceability — the `<gmailMessageId>__` prefix on any file in Drive names
  // the exact email it came from.
  if (folder.getFilesByName(filename).hasNext()) {
    Logger.log("SKIP attachment: " + filename + " already exists in Drive Inbox/.");
    return false;
  }

  // Name the BLOB before createFile rather than renaming the File afterwards:
  // createFile takes the name from the blob, so this is the only way the file is
  // created with its final name. Renaming after creation leaves a window in
  // which the pipeline's poll could pick the file up under the raw attachment
  // name, defeating the check above. copyBlob() because the docs describe the
  // attachment as "a regular Blob" without naming the interface it implements.
  var blob = attachment.copyBlob().setName(filename);
  var file = folder.createFile(blob);

  Logger.log(
    "SAVED message=" +
      messageId +
      " subject=" +
      JSON.stringify(subject) +
      " from=" +
      message.getFrom() +
      " date=" +
      message.getDate() +
      " attachment=" +
      JSON.stringify(attachment.getName()) +
      " bytes=" +
      attachment.getSize() +
      " -> driveFile=" +
      file.getId() +
      " (" +
      file.getName() +
      ")"
  );

  return true;
}

function isPdf_(attachment) {
  // Content type first, filename as the fallback: a client that sends a deck as
  // application/octet-stream still gives it a .pdf name, and a client that drops
  // the extension still sets the content type. Filename alone is not enough.
  var name = attachment.getName() || "";
  return attachment.getContentType() === "application/pdf" || /\.pdf$/i.test(name);
}

function attachmentName_(attachment, index) {
  var raw = attachment.getName();
  if (!raw) {
    // A nameless attachment is valid MIME. The index keeps two nameless PDFs on
    // one message from colliding on the deterministic filename above.
    return "attachment-" + (index + 1) + ".pdf";
  }
  // Strip only what would break a Drive filename or a log line. Spaces stay, so
  // the file still reads like the deck it came from.
  var cleaned = raw.replace(/[\/\\\r\n\t]+/g, "_").replace(/^\s+|\s+$/g, "");
  return cleaned || "attachment-" + (index + 1) + ".pdf";
}

function threadHasLabel_(thread, labelName) {
  // Labels are a THREAD-level thing in Apps Script: addLabel/removeLabel exist
  // on GmailThread, not on GmailMessage.
  var labels = thread.getLabels();
  for (var i = 0; i < labels.length; i++) {
    if (labels[i].getName() === labelName) {
      return true;
    }
  }
  return false;
}

function getOrCreateLabel_(labelName) {
  // getUserLabelByName returns null rather than throwing when the label does not
  // exist, so the first run in a fresh mailbox creates it.
  var label = GmailApp.getUserLabelByName(labelName);
  if (label) {
    return label;
  }
  Logger.log("Creating Gmail label " + labelName + " (first run).");
  return GmailApp.createLabel(labelName);
}

function describeError_(err) {
  if (!err) {
    return "unknown error";
  }
  return String(err.stack || err.message || err);
}

// ---------------------------------------------------------------------------
// ONE-TIME SETUP HELPER — run this by hand, once.
// ---------------------------------------------------------------------------

/**
 * Installs the time-driven trigger so you do not have to click through the
 * Triggers UI.
 *
 * The UI route (Triggers -> Add Trigger) is still worth knowing, because it also
 * sets the failure-notification preference — set it to "Notify me immediately".
 * A background trigger's exceptions surface nowhere at all otherwise: no email,
 * no UI, just a line in Executions that nobody is looking at. This helper cannot
 * set that preference; see docs/gmail-intake-setup.md.
 */
function setUpTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  var removed = 0;
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === HANDLER_FUNCTION) {
      // Running this twice would otherwise leave two triggers firing the same
      // handler. Harmless for correctness — the lock and the three idempotency
      // layers hold — but it doubles the daily runtime quota this script burns.
      ScriptApp.deleteTrigger(existing[i]);
      removed++;
    }
  }

  ScriptApp.newTrigger(HANDLER_FUNCTION)
    .timeBased()
    .everyMinutes(TRIGGER_INTERVAL_MINUTES)
    .create();

  Logger.log(
    "Trigger installed: " +
      HANDLER_FUNCTION +
      " every " +
      TRIGGER_INTERVAL_MINUTES +
      " minute(s)" +
      (removed ? " (replaced " + removed + " existing trigger(s))" : "") +
      ". NOTE: this does NOT set failure notifications — open Triggers and set " +
      '"Notify me immediately" on it, or see docs/gmail-intake-setup.md.'
  );
}
