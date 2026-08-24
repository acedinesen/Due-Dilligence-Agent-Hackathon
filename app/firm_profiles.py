from __future__ import annotations

import json
from pathlib import Path

from app.models import FirmProfile


PROFILE_DIR = Path(__file__).resolve().parent.parent / "firm_profiles"


def load_firm(profile_id: str | None) -> FirmProfile | None:
    if not profile_id:
        return None

    path = PROFILE_DIR / f"{profile_id}.json"
    if not path.exists():
        raise FileNotFoundError(profile_id)

    return FirmProfile.model_validate(json.loads(path.read_text(encoding="utf-8")))
