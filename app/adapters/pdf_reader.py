from __future__ import annotations

import httpx

from app.config import settings
from app.models import DeckPage, ParsedDeck


class PdfReaderClient:
    """Single adapter for the separate Railway PDF reader."""

    async def parse(self, pdf_bytes: bytes, filename: str) -> ParsedDeck:
        url = settings.pdf_reader_url.rstrip("/") + "/" + settings.pdf_reader_path.lstrip("/")

        async with httpx.AsyncClient(timeout=settings.pdf_reader_timeout_seconds) as client:
            response = await client.post(
                url,
                files={"file": (filename, pdf_bytes, "application/pdf")},
            )
            response.raise_for_status()
            payload = response.json()

        return self._normalize(payload, filename)

    def _normalize(self, payload: dict, filename: str) -> ParsedDeck:
        # Change only this method if the PDF reader returns a different shape.
        full_text = payload.get("text") or payload.get("full_text") or payload.get("content") or ""
        raw_pages = payload.get("pages") or []
        pages: list[DeckPage] = []

        for index, item in enumerate(raw_pages, start=1):
            if isinstance(item, str):
                pages.append(DeckPage(page=index, text=item))
            elif isinstance(item, dict):
                pages.append(
                    DeckPage(
                        page=int(item.get("page") or item.get("page_number") or index),
                        text=str(item.get("text") or item.get("content") or ""),
                    )
                )

        if not full_text and pages:
            full_text = "\n\n".join(page.text for page in pages)

        if not full_text:
            raise ValueError("PDF reader returned no usable text")

        return ParsedDeck(
            filename=filename,
            full_text=full_text,
            pages=pages,
            metadata=payload.get("metadata") or {},
        )
