from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Keepitsimple Due Diligence"

    # Existing Railway PDF ingestion service.
    pdf_reader_url: str = "https://pdfreader-production-29d1.up.railway.app"
    # Verified live against the service's own OpenAPI schema (2026-08-24): the
    # Railway reader exposes exactly one route, `POST /extract`. `/parse` returns
    # 404, which app/pipeline.py swallows per-file, so every deck silently sat in
    # Drive Inbox/ forever. Kept configurable because the query string rides along
    # here: `images` defaults to `all`, which returns per-page base64 JPEGs that
    # PdfReaderClient._normalize() throws away (measured 542,777 bytes vs 3,469
    # for the same 6-page text deck), so triage pays for a payload it never reads.
    pdf_reader_path: str = "/extract?images=none"
    pdf_reader_timeout_seconds: float = 90.0

    # Used by both Claude calls: Track A's cheap triage pre-filter
    # (app/triage.py) and Track B's deep-research agent (app/diligence.py).
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    # Track C — Attio CRM delivery (app/adapters/attio_client.py).
    attio_api_key: str | None = None

    # Track C — Slack delivery (app/adapters/slack_notifier.py).
    # Preferred: an incoming webhook. The URL is itself the credential (no
    # auth header, no channel argument), so it must never be logged.
    slack_webhook_url: str | None = None
    # Alternative: a bot token posting via chat.postMessage. Both of these are
    # needed together; a webhook URL, if set, wins over them.
    slack_bot_token: str | None = None
    slack_channel_id: str | None = None

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
