from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Keepitsimple Due Diligence"

    # Existing Railway PDF ingestion service.
    pdf_reader_url: str = "https://pdfreader-production-29d1.up.railway.app"
    # Keep configurable until the reader's exact route is confirmed.
    pdf_reader_path: str = "/parse"
    pdf_reader_timeout_seconds: float = 90.0

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    # Triage LLM call (app/triage.py) — routed through OpenRouter instead of
    # Anthropic directly, so a free model can be used for the cheap pre-filter.
    openrouter_api_key: str | None = None
    openrouter_model: str = "nvidia/nemotron-3.5-lightning:free"

    # Track C — Attio CRM delivery (app/adapters/attio_client.py).
    attio_api_key: str | None = None

    supabase_url: str | None = None
    supabase_key: str | None = None
    supabase_bucket: str = "pitch-decks"

    # Track A — Google Drive trigger, triage & flagging.
    # Raw JSON content of a service-account key (not a file path), so it can
    # be set as a single Railway env var without a mounted file.
    google_service_account_json: str | None = None
    drive_inbox_id: str | None = None
    drive_relevant_id: str | None = None
    drive_review_id: str | None = None
    drive_not_relevant_id: str | None = None
    pipeline_firm_profile: str = "generic_seed"
    pipeline_poll_interval_seconds: float = 45.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
