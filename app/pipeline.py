from __future__ import annotations

import asyncio
import logging
from typing import Literal

from app.adapters.attio_client import save_to_attio
from app.adapters.drive_store import DriveStore
from app.adapters.pdf_reader import PdfReaderClient
from app.adapters.slack_notifier import send_slack_notification
from app.agent import DiligenceAgent
from app.config import settings
from app.diligence import ClaudeDiligenceAgent
from app.firm_profiles import load_firm
from app.models import FirmProfile
from app.triage import TriageAgent

logger = logging.getLogger("pipeline")


def _dest_folder_for_flag(flag: Literal["relevant", "review", "not_relevant"]) -> str | None:
    return {
        "relevant": settings.drive_relevant_id,
        "review": settings.drive_review_id,
        "not_relevant": settings.drive_not_relevant_id,
    }.get(flag)


async def run_once(
    drive_store: DriveStore,
    pdf_reader: PdfReaderClient,
    triage_agent: TriageAgent,
    diligence_agent: DiligenceAgent,
    firm: FirmProfile | None,
) -> None:
    """One poll cycle: list Inbox/, then parse + triage + move + deep-dive + deliver.

    Failure isolation is per-file and per-stage: nothing in here may abort the
    poll loop or the rest of the batch, and no failure may silently discard a
    finished report (see the delivery handlers below). The two deliveries —
    Attio and Slack — are independent of each other: either can fail without
    suppressing the other.
    """
    inbox_files = await asyncio.to_thread(drive_store.list_inbox, settings.drive_inbox_id)

    for file in inbox_files:
        file_id = file["id"]
        filename = file.get("name", file_id)
        logger.info("Processing %s (%s)", filename, file_id)

        try:
            pdf_bytes = await asyncio.to_thread(drive_store.download, file_id)
            deck = await pdf_reader.parse(pdf_bytes, filename)
            triage = await triage_agent.classify(deck, firm)

            dest_folder_id = _dest_folder_for_flag(triage.flag)
            if dest_folder_id:
                # Deliberately filed BEFORE the deep dive, not after. Inbox/ is the
                # work queue: `list_inbox` is the only thing that decides what gets
                # processed, so a file left in Inbox is re-downloaded, re-triaged and
                # re-analyzed on every cycle. At ~$1 and ~5 minutes per deep dive and
                # a 45s poll interval, that is an unbounded spend on a single bad
                # deck. Moving first means the file is filed by its (already final)
                # triage flag exactly once, and any later failure leaves the file
                # correctly filed but without an Attio record — recoverable from the
                # ERROR logs below, which is the cheaper of the two bad outcomes.
                # A failing `move` raises here, before any money is spent, and the
                # deck is retried next cycle.
                await asyncio.to_thread(drive_store.move, file_id, dest_folder_id)
            else:
                logger.warning(
                    "No destination folder configured for flag %r — leaving %s in Inbox "
                    "(it will be re-processed, including the deep dive, next cycle)",
                    triage.flag,
                    filename,
                )

            if triage.flag == "not_relevant":
                # The whole cost model of the pipeline lives on this branch: a
                # not_relevant deck must never reach the diligence agent or Attio,
                # so it costs one cheap triage call and nothing else.
                logger.info(
                    "SKIPPED downstream: %s flagged not_relevant (%s)", filename, triage.reason
                )
                continue

            logger.info("%s flagged %s (%s)", filename, triage.flag, triage.reason)

            # ---- Track B: deep dive -------------------------------------------
            # The expensive half (~5 min, ~$1 per deck). Isolated so that one
            # deck's analysis blowing up (DiligenceError, model/transport error,
            # anything) costs us this deck only, never the batch or the loop.
            try:
                logger.info("Starting deep-dive analysis for %s", filename)
                report = await diligence_agent.analyze(deck, firm)
            except Exception:
                logger.exception(
                    "Deep-dive analysis failed for %s (%s) — file is already filed "
                    "under %r in Drive, no Attio record was created. Nothing to "
                    "recover: no report was produced.",
                    filename,
                    file_id,
                    triage.flag,
                )
                continue

            logger.info(
                "Deep dive complete for %s — company=%r, findings=%d",
                filename,
                report.company.name,
                len(report.key_findings),
            )

            # ---- Track C: delivery --------------------------------------------
            # Worst case in the whole pipeline: the analysis above already cost
            # real money and ~5 minutes, so no delivery error may take the report
            # with it. Two deliveries, two independent try/excepts, neither
            # gating the other:
            #   * The CRM record is the durable artefact.
            #   * The Slack message is how a human finds out the deck was
            #     processed at all, and it carries only company name, one-liner,
            #     founder bios and the website link — every one of which comes
            #     off the report itself, nothing from Attio. So a CRM outage must
            #     not silence the notification, and a Slack outage must not hide
            #     a CRM record that was written fine.
            # No retry loop here on purpose — both adapters already retry
            # internally where retrying is safe.
            attio_saved = False
            try:
                attio_url = await save_to_attio(report)
            except Exception:
                logger.exception(
                    "Attio save FAILED for %s (%s) after a successful analysis — "
                    "file is already filed under %r in Drive. Slack delivery is "
                    "still attempted below. The report dumped next is the only "
                    "copy; re-deliver it manually.",
                    filename,
                    file_id,
                    triage.flag,
                )
                logger.error(
                    "UNSAVED REPORT %s (%s): %s",
                    report.company.name,
                    filename,
                    report.model_dump_json(),
                )
            else:
                attio_saved = True
                if attio_url:
                    logger.info("Attio record for %s: %s", report.company.name, attio_url)
                else:
                    logger.warning(
                        "Attio save for %s returned no web_url — the record may exist "
                        "but cannot be linked to from here",
                        report.company.name,
                    )

            try:
                await send_slack_notification(report)
            except Exception:
                # Deliberately asymmetric with the Attio handler above: the
                # recovery logging is sized to what is actually at risk. If Attio
                # holds the report, a failed notification loses nothing but the
                # ping, so it gets one loud line — dumping the ~18KB report JSON
                # again would bury the logs for a cosmetic failure. If Attio
                # failed too, the UNSAVED REPORT dump above is already there,
                # exactly once, which is what a human needs to re-deliver by
                # hand. Either way the report is never silently discarded.
                logger.exception(
                    "Slack notification FAILED for %s (%s) — %s",
                    filename,
                    file_id,
                    (
                        "the report is saved in Attio, so this is a "
                        "notification-only failure"
                        if attio_saved
                        else "the Attio save failed too — recover the report from "
                        "the UNSAVED REPORT line above"
                    ),
                )
            else:
                logger.info("Slack notification sent for %s", report.company.name)
        except Exception:
            # Catch-all for the cheap stages (download / parse / triage / move) and
            # for genuine bugs. These decks stay in Inbox and are retried next
            # cycle, which is safe: nothing expensive has run yet.
            logger.exception(
                "Failed to process %s (%s) — leaving in Inbox for manual inspection",
                filename,
                file_id,
            )
            continue


def _require_drive_folder_settings() -> None:
    """Fail fast at startup if any required Drive folder ID is unset.

    Blank counts as unset, and that is the whole point of this function. A bare
    `DRIVE_REVIEW_ID=` line in .env reaches pydantic-settings as `""`, not
    `None`, so a `value is None` test passes it. The failure downstream is
    silent and expensive: `_dest_folder_for_flag` returns `""`, the falsy
    `if dest_folder_id:` skips the move, and the deck is left in Inbox/ — where
    `list_inbox` finds it again every 45s and re-runs the ~$1 / ~5 min deep dive
    on it forever. Catching it here, before the loop starts, is the only cheap
    place to catch it.
    """
    required = {
        "DRIVE_INBOX_ID": settings.drive_inbox_id,
        "DRIVE_RELEVANT_ID": settings.drive_relevant_id,
        "DRIVE_REVIEW_ID": settings.drive_review_id,
        "DRIVE_NOT_RELEVANT_ID": settings.drive_not_relevant_id,
    }
    missing = [name for name, value in required.items() if not (value or "").strip()]
    if missing:
        raise RuntimeError(
            "Missing or blank required Drive folder setting(s): "
            f"{', '.join(missing)}. Set each to a real Drive folder ID (see "
            "scripts/setup_drive_folders.py) — a present-but-empty value would "
            "leave decks in Inbox/ and re-run the deep dive on every poll."
        )


async def poll_forever() -> None:
    # Configured here, not at import time: importing this module should not
    # reconfigure root logging for whatever process happens to import it
    # (FastAPI, a test run, another script).
    logging.basicConfig(level=logging.INFO)
    _require_drive_folder_settings()

    firm = load_firm(settings.pipeline_firm_profile)
    drive_store = DriveStore()
    pdf_reader = PdfReaderClient()
    triage_agent = TriageAgent()
    diligence_agent = ClaudeDiligenceAgent()

    logger.info(
        "Starting Drive poll loop (interval=%ss, firm=%s)",
        settings.pipeline_poll_interval_seconds,
        settings.pipeline_firm_profile,
    )

    while True:
        try:
            await run_once(drive_store, pdf_reader, triage_agent, diligence_agent, firm)
        except Exception:
            logger.exception("Error during poll cycle")

        await asyncio.sleep(settings.pipeline_poll_interval_seconds)


if __name__ == "__main__":
    asyncio.run(poll_forever())
