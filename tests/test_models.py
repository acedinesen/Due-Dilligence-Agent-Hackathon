import pytest
from pydantic import ValidationError

from app.models import Source, SourceType


def test_external_evidence_requires_url():
    with pytest.raises(ValidationError):
        Source(
            type=SourceType.EXTERNAL,
            title="Market report",
            evidence="Supports the TAM claim.",
        )


def test_deck_evidence_requires_page():
    with pytest.raises(ValidationError):
        Source(
            type=SourceType.DECK,
            title="Pitch deck",
            evidence="Deck claim.",
        )
