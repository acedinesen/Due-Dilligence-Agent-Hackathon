from __future__ import annotations

from uuid import uuid4

from app.config import settings
from app.models import ParsedDeck


class SupabaseStore:
    """Small persistence adapter. Disabled automatically when env vars are missing."""

    def __init__(self):
        self.enabled = bool(settings.supabase_url and settings.supabase_key)
        self.client = None
        if self.enabled:
            from supabase import create_client
            self.client = create_client(settings.supabase_url, settings.supabase_key)

    def save(self, pdf_bytes: bytes, deck: ParsedDeck) -> str | None:
        if not self.enabled or self.client is None:
            return None

        deck_id = str(uuid4())
        storage_path = f"{deck_id}/{deck.filename}"

        self.client.storage.from_(settings.supabase_bucket).upload(
            storage_path,
            pdf_bytes,
            {"content-type": "application/pdf"},
        )

        self.client.table("pitch_decks").insert(
            {
                "id": deck_id,
                "filename": deck.filename,
                "storage_path": storage_path,
                "full_text": deck.full_text,
                "pages": [page.model_dump() for page in deck.pages],
                "parser_metadata": deck.metadata,
            }
        ).execute()

        return deck_id
