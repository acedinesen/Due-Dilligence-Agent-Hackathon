from __future__ import annotations

import logging

from anthropic import APIStatusError
from fastapi import FastAPI, File, HTTPException, Query, UploadFile

from app.adapters.attio_client import save_to_attio
from app.adapters.pdf_reader import PdfReaderClient
from app.adapters.supabase_store import SupabaseStore
from app.config import settings
from app.diligence import ClaudeDiligenceAgent, DiligenceError
from app.firm_profiles import load_firm

logger = logging.getLogger("api")

app = FastAPI(title=settings.app_name, version="0.2.0")
pdf_reader = PdfReaderClient()
store = SupabaseStore()
# The real deep-dive agent (was MockDiligenceAgent). Its Anthropic client is
# built lazily, so constructing it here never needs an API key at import time.
agent = ClaudeDiligenceAgent()


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/parse")
async def parse(file: UploadFile = File(...)):
    pdf_bytes = await file.read()
    try:
        return await pdf_reader.parse(pdf_bytes, file.filename or "pitch-deck.pdf")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"PDF reader failed: {exc}") from exc


@app.post("/analyze")
async def analyze(
    file: UploadFile = File(...),
    firm: str | None = Query(default=None),
    attio: bool = Query(
        default=False,
        description=(
            "Also push the finished report to Attio. Off by default: this is the "
            "human-triggered path, and iterating on a deck here should not write "
            "to the team CRM. The Drive pipeline always delivers."
        ),
    ),
):
    """Run the full deep dive on an uploaded deck.

    NOTE: this is a slow, expensive endpoint — one real run is roughly 5 minutes
    and ~$1 of tokens (two Claude calls, one with server-side web search). Any
    proxy in front of this app needs a request timeout well above 5 minutes, or
    the client will see a gateway timeout while the run keeps going server-side.
    """
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="Upload a PDF")

    try:
        firm_profile = load_firm(firm)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Unknown firm profile: {firm}")

    # Pre-flight the one piece of config that would otherwise fail *after* the
    # upload, the parse and the Supabase write — and would previously have
    # surfaced as an opaque 500.
    if not (settings.anthropic_api_key or "").strip():
        raise HTTPException(
            status_code=503,
            detail=(
                "ANTHROPIC_API_KEY is not set, so the diligence agent cannot run. "
                "Set it in the server environment (.env / Railway) and retry. "
                "Nothing is wrong with the upload."
            ),
        )

    pdf_bytes = await file.read()

    try:
        deck = await pdf_reader.parse(pdf_bytes, file.filename or "pitch-deck.pdf")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"PDF reader failed: {exc}") from exc

    try:
        deck_id = store.save(pdf_bytes, deck)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Deck storage failed: {exc}") from exc

    try:
        report = await agent.analyze(deck, firm_profile)
    except DiligenceError as exc:
        # The agent's own fatal condition: no valid, evidence-backed report could
        # be produced (bad/missing credentials, model refused the schema, ...).
        # Its message is written to be read by a human, so pass it through
        # verbatim rather than collapsing it into a generic 500.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except APIStatusError as exc:
        # e.g. a present-but-invalid key (401) or a rate limit (429) — the
        # pre-flight above cannot catch these, only Anthropic can.
        raise HTTPException(
            status_code=502,
            detail=f"Anthropic API error ({exc.status_code}): {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc

    response: dict = {"deck_id": deck_id, "report": report}

    if attio:
        # Same rule as the pipeline: the analysis has already been paid for, so a
        # CRM failure reports itself in the response instead of turning a finished
        # report into a 5xx with no body.
        try:
            response["attio_url"] = await save_to_attio(report)
        except Exception as exc:
            logger.exception("Attio save failed for %s after a successful analysis", deck_id)
            response["attio_url"] = None
            response["attio_error"] = str(exc)

    return response
