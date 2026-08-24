"""Startup guard for the Drive folder IDs (app/pipeline.py).

Kept in its own file rather than a general test_pipeline.py so it does not
collide with anyone else's pipeline work in progress.

The bug this locks down: a bare `DRIVE_REVIEW_ID=` line in .env arrives as `""`,
not `None`. The old `value is None` guard let it through, then
`_dest_folder_for_flag` returned `""`, the falsy `if dest_folder_id:` skipped
the Drive move, and the deck stayed in Inbox/ — re-triaged and re-deep-dived
(~$1, ~5 min) on every 45s poll, silently.
"""

from __future__ import annotations

import pytest

from app import pipeline
from app.pipeline import _require_drive_folder_settings

_FOLDER_SETTINGS = {
    "drive_inbox_id": "DRIVE_INBOX_ID",
    "drive_relevant_id": "DRIVE_RELEVANT_ID",
    "drive_review_id": "DRIVE_REVIEW_ID",
    "drive_not_relevant_id": "DRIVE_NOT_RELEVANT_ID",
}


@pytest.fixture
def all_folders_set(monkeypatch):
    for attr in _FOLDER_SETTINGS:
        monkeypatch.setattr(pipeline.settings, attr, f"id-for-{attr}")


def test_all_folder_ids_present_passes(all_folders_set):
    _require_drive_folder_settings()


@pytest.mark.parametrize("attr,env_name", sorted(_FOLDER_SETTINGS.items()))
@pytest.mark.parametrize("blank", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_blank_folder_id_is_rejected(all_folders_set, monkeypatch, attr, env_name, blank):
    monkeypatch.setattr(pipeline.settings, attr, blank)

    with pytest.raises(RuntimeError) as excinfo:
        _require_drive_folder_settings()

    assert env_name in str(excinfo.value)


@pytest.mark.parametrize("attr,env_name", sorted(_FOLDER_SETTINGS.items()))
def test_unset_folder_id_is_rejected(all_folders_set, monkeypatch, attr, env_name):
    monkeypatch.setattr(pipeline.settings, attr, None)

    with pytest.raises(RuntimeError) as excinfo:
        _require_drive_folder_settings()

    assert env_name in str(excinfo.value)


def test_error_names_every_offending_setting(all_folders_set, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "drive_review_id", "")
    monkeypatch.setattr(pipeline.settings, "drive_not_relevant_id", None)

    with pytest.raises(RuntimeError) as excinfo:
        _require_drive_folder_settings()

    message = str(excinfo.value)
    assert "DRIVE_REVIEW_ID" in message
    assert "DRIVE_NOT_RELEVANT_ID" in message
    assert "DRIVE_INBOX_ID" not in message


def test_blank_would_otherwise_reach_a_falsy_destination_folder(monkeypatch):
    """Why blank must be caught at startup, stated as a test.

    `_dest_folder_for_flag` hands `run_once` a falsy folder id, which skips the
    move — the exact silent path the guard exists to make impossible.
    """
    monkeypatch.setattr(pipeline.settings, "drive_review_id", "")

    assert not pipeline._dest_folder_for_flag("review")
