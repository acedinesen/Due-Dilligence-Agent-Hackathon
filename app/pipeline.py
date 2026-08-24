from __future__ import annotations

import asyncio
import logging
from typing import Literal

from app.adapters.drive_store import DriveStore
from app.adapters.pdf_reader import PdfReaderClient
from app.config import settings
from app.firm_profiles import load_firm
from app.models import FirmProfile
from app.triage import TriageAgent

logging.basicConfig(level=logging.INFO)
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
    firm: FirmProfile | None,
) -> None:
    """One poll cycle: list Inbox/, parse + triage + move each file found."""
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
                await asyncio.to_thread(drive_store.move, file_id, dest_folder_id)
            else:
                logger.warning(
                    "No destination folder configured for flag %r — leaving %s in Inbox",
                    triage.flag,
                    filename,
                )

            if triage.flag == "not_relevant":
                logger.info(
                    "SKIPPED downstream: %s flagged not_relevant (%s)", filename, triage.reason
                )
                continue

            logger.info("%s flagged %s (%s)", filename, triage.flag, triage.reason)
            # Track B handoff — not wired in yet. Once the deep-dive agent exists,
            # call it here for relevant/review decks:
            # report = await diligence_agent.analyze(deck, firm)
        except Exception:
            logger.exception(
                "Failed to process %s (%s) — leaving in Inbox for manual inspection",
                filename,
                file_id,
            )
            continue


def _require_drive_folder_settings() -> None:
    """Fail fast at startup if any required Drive folder ID is unset."""
    required = {
        "DRIVE_INBOX_ID": settings.drive_inbox_id,
        "DRIVE_RELEVANT_ID": settings.drive_relevant_id,
        "DRIVE_REVIEW_ID": settings.drive_review_id,
        "DRIVE_NOT_RELEVANT_ID": settings.drive_not_relevant_id,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise RuntimeError(
            f"Missing required Drive folder setting(s): {', '.join(missing)}"
        )


async def poll_forever() -> None:
    _require_drive_folder_settings()

    firm = load_firm(settings.pipeline_firm_profile)
    drive_store = DriveStore()
    pdf_reader = PdfReaderClient()
    triage_agent = TriageAgent()

    logger.info(
        "Starting Drive poll loop (interval=%ss, firm=%s)",
        settings.pipeline_poll_interval_seconds,
        settings.pipeline_firm_profile,
    )

    while True:
        try:
            await run_once(drive_store, pdf_reader, triage_agent, firm)
        except Exception:
            logger.exception("Error during poll cycle")

        await asyncio.sleep(settings.pipeline_poll_interval_seconds)


if __name__ == "__main__":
    asyncio.run(poll_forever())
