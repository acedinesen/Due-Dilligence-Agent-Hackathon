from __future__ import annotations

from fastapi import FastAPI, File, HTTPException, Query, UploadFile

from app.adapters.pdf_reader import PdfReaderClient
from app.adapters.supabase_store import SupabaseStore
from app.agent import MockDiligenceAgent
from app.config import settings
from app.firm_profiles import load_firm


app = FastAPI(title=settings.app_name, version="0.2.0")
pdf_reader = PdfReaderClient()
store = SupabaseStore()
agent = MockDiligenceAgent()  # replace with ClaudeDiligenceAgent later


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
):
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="Upload a PDF")

    try:
        firm_profile = load_firm(firm)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Unknown firm profile: {firm}")

    pdf_bytes = await file.read()

    try:
        deck = await pdf_reader.parse(pdf_bytes, file.filename or "pitch-deck.pdf")
        deck_id = store.save(pdf_bytes, deck)
        report = await agent.analyze(deck, firm_profile)
        return {"deck_id": deck_id, "report": report}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
