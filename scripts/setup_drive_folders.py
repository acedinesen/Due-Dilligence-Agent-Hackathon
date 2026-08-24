"""One-time setup helper — NOT a test, run by a human.

Creates the four Track A Drive subfolders (Inbox, Relevant, Review,
Not-Relevant) inside a parent folder the human has already created and
shared with the service account as Editor.

Usage:
    python3 scripts/setup_drive_folders.py <parent_folder_id>

Prints each created folder's name and ID so they can be pasted into `.env`
as DRIVE_INBOX_ID / DRIVE_RELEVANT_ID / DRIVE_REVIEW_ID / DRIVE_NOT_RELEVANT_ID.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.config import settings

SCOPES = ["https://www.googleapis.com/auth/drive"]

FOLDER_NAMES = ["Inbox", "Relevant", "Review", "Not-Relevant"]


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} <parent_folder_id>", file=sys.stderr)
        sys.exit(1)

    parent_id = sys.argv[1]

    if not settings.google_service_account_json:
        print(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not set — set it (e.g. in .env) before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    info = json.loads(settings.google_service_account_json)
    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    service = build("drive", "v3", credentials=credentials)

    for name in FOLDER_NAMES:
        folder = (
            service.files()
            .create(
                body={
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                },
                fields="id, name",
            )
            .execute()
        )
        print(f"{folder['name']}: {folder['id']}")


if __name__ == "__main__":
    main()
