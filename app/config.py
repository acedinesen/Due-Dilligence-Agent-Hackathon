from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Keepitsimple Due Diligence"

    # Existing Railway PDF ingestion service.
    pdf_reader_url: str = "https://pdfreader-production-29d1.up.railway.app"
    # Keep configurable until the reader's exact route is confirmed.
    pdf_reader_path: str = "/parse"
    pdf_reader_timeout_seconds: float = 90.0

    anthropic_api_key: str | None = None
    anthropic_model: str = ""

    supabase_url: str | None = None
    supabase_key: str | None = None
    supabase_bucket: str = "pitch-decks"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
