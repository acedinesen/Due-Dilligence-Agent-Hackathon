from __future__ import annotations

import json

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

from app.config import settings

SCOPES = ["https://www.googleapis.com/auth/drive"]


class DriveStore:
    """Thin adapter over the Google Drive REST API v3 (service account auth).

    Mirrors the style of app/adapters/pdf_reader.py: one clear responsibility,
    isolated so the Drive mechanics can change without touching the rest of
    the app. This talks to the real Drive API directly (googleapiclient) —
    it does NOT go through any Claude/MCP tool, since this process runs
    standalone (outside a live Claude Code session).
    """

    def __init__(self) -> None:
        if not settings.google_service_account_json:
            raise ValueError(
                "google_service_account_json is not set — cannot build a Drive client"
            )

        info = json.loads(settings.google_service_account_json)
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
        self._service = build("drive", "v3", credentials=credentials)

    def list_inbox(self, inbox_folder_id: str) -> list[dict]:
        """Lists files currently sitting in the given inbox folder."""
        response = (
            self._service.files()
            .list(
                q=f"'{inbox_folder_id}' in parents and trashed=false",
                fields="files(id,name)",
            )
            .execute()
        )
        return response.get("files", [])

    def download(self, file_id: str) -> bytes:
        """Downloads a file's raw bytes."""
        request = self._service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def move(self, file_id: str, dest_folder_id: str) -> None:
        """Moves a file by reparenting it — the only move primitive Drive has."""
        current = self._service.files().get(fileId=file_id, fields="parents").execute()
        previous_parents = ",".join(current.get("parents", []))
        self._service.files().update(
            fileId=file_id,
            addParents=dest_folder_id,
            removeParents=previous_parents,
            fields="id, parents",
        ).execute()
