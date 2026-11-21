"""Runtime settings, read from environment variables or ``backend/.env``."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Comma-separated origins allowed to call the API from a browser.
    frontend_origins: str = "http://localhost:3000"

    # Which AI provider reads screenshots. "auto" uses Anthropic when its key is set, otherwise
    # Gemini when its key is set. Keys stay on the server; the browser never sees them.
    extraction_provider: Literal["auto", "anthropic", "gemini"] = "auto"
    extraction_timeout_seconds: float = 180.0

    # Anthropic (Claude)
    anthropic_api_key: str | None = None
    extraction_model: str = "claude-opus-5-5"
    extraction_effort: str = "high"
    # Re-run a request on Anthropic's recommended fallback model if the first model declines it.
    extraction_refusal_fallback: bool = True
    # Only used to estimate cost in logs (USD per million tokens).
    extraction_input_price_per_mtok: float = 4.0
    extraction_output_price_per_mtok: float = 20.0

    # Google (Gemini). The key is read from GEMINI_API_KEY.
    gemini_api_key: str | None = None
    # 2.5 Flash answered every evaluation case in 8-16s on 2026-11-19, while 3.5 Flash took up to 74s
    # and often returned 503 or rate limits. See evals/README.md.
    gemini_model: str = "gemini-2.5-flash"
    # Gemini 3 models accept "low" or "high"; empty leaves the model's default.
    gemini_thinking_level: Literal["", "low", "high"] = ""
    # Answers instead of the configured model when that one is overloaded (5xx) or doesn't answer
    # within gemini_primary_timeout_seconds. Empty disables it, and then a 5xx is retried once.
    gemini_fallback_model: str = ""
    gemini_primary_timeout_seconds: float = 60.0
    # Answers short text tasks, such as reading a typed WhatsApp correction, so they don't use the
    # main model's daily quota (20 requests per model on the free tier). Empty uses the main model.
    gemini_light_model: str = "gemini-2.5-flash-lite"
    # Only used to estimate cost in logs. Zero suits the free tier; set your plan's prices otherwise.
    gemini_input_price_per_mtok: float = 0.0
    gemini_output_price_per_mtok: float = 0.0

    # Uploads
    max_files: int = 10
    max_file_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 40 * 1024 * 1024
    # WhatsApp chat exports: the whole file (a ZIP may hold photos and voice notes), and its chat text.
    # Only the last 600 messages are read, so a longer text isn't worth accepting.
    max_chat_export_bytes: int = 30 * 1024 * 1024
    max_chat_text_bytes: int = 4 * 1024 * 1024

    # Rate limits, per client IP, kept in memory.
    extraction_rate_limit: int = 12
    extraction_rate_window_seconds: float = 600
    document_rate_limit: int = 120
    document_rate_window_seconds: float = 600
    calculate_rate_limit: int = 1200
    calculate_rate_window_seconds: float = 600
    # Only enable behind a proxy you control; otherwise clients can spoof X-Forwarded-For.
    trust_proxy_headers: bool = False
    # With trust_proxy_headers: a header the proxy sets with the visitor's address, such as
    # cf-connecting-ip on Render. Empty uses the last X-Forwarded-For entry.
    client_ip_header: str = ""

    # Document rendering. "" uses Playwright's bundled Chromium; "msedge" or "chrome" use an
    # installed browser instead.
    browser_channel: str = ""
    document_timeout_seconds: float = 30.0

    # ---- Saved records: the WhatsApp channel and the /records page ----
    # PostgreSQL (Supabase or any other). Without it, WhatsApp and /records report that they are
    # unavailable and the website's upload workflow keeps working as before.
    database_url: SecretStr | None = None
    database_pool_min_size: int = 1
    database_pool_max_size: int = 8
    database_statement_timeout_ms: int = 15_000

    # WhatsApp Cloud API credentials, from the Meta app dashboard.
    whatsapp_access_token: SecretStr | None = None
    whatsapp_phone_number_id: str = ""
    # ChatReceipt's own WhatsApp number as people dial it, in international form (2348012345678). It
    # goes into the link on each receipt that lets the other person confirm it; without it, receipts
    # carry no link.
    whatsapp_display_number: str = ""
    # Signs every webhook delivery; deliveries that don't verify against it are refused.
    whatsapp_app_secret: SecretStr | None = None
    # A value you choose and enter in the dashboard when registering the webhook.
    whatsapp_verify_token: SecretStr | None = None
    whatsapp_graph_base_url: str = "https://graph.facebook.com"
    whatsapp_graph_api_version: str = "v26.0"
    whatsapp_timeout_seconds: float = 20.0

    # Background work. By default a worker runs inside the API process; set this to false and run
    # `python -m app.whatsapp.worker` to run it separately.
    whatsapp_worker_in_process: bool = True
    whatsapp_poll_seconds: float = 1.0
    # Screenshots sent together arrive as separate messages. Wait this long after the latest one
    # (and no longer than the maximum after the first) before reading them as one conversation.
    whatsapp_album_wait_seconds: float = 6.0
    whatsapp_album_max_wait_seconds: float = 20.0
    whatsapp_extraction_concurrency: int = 2
    whatsapp_extraction_max_attempts: int = 4
    whatsapp_extraction_lease_seconds: float = 300.0
    # The customers' time zone: what "today" means for dates such as a chat's "Yesterday".
    whatsapp_timezone: str = "Africa/Lagos"
    # Retries finished sooner than this don't need a "taking longer than usual" message.
    whatsapp_delay_notice_seconds: float = 30.0
    # How long to wait for the AI to understand a typed correction before answering without it.
    whatsapp_understanding_timeout_seconds: float = 45.0
    whatsapp_send_max_attempts: int = 8
    whatsapp_retry_base_seconds: float = 10.0
    whatsapp_retry_max_seconds: float = 900.0

    # Abuse limits, per customer.
    whatsapp_images_per_hour: int = 30
    whatsapp_images_per_day: int = 100
    whatsapp_messages_per_minute: int = 30
    # Drafts nobody confirmed are deleted after this long.
    whatsapp_draft_ttl_hours: int = 72

    # The website address used in links sent over WhatsApp. Empty uses the first FRONTEND_ORIGINS.
    public_web_url: str = ""
    records_sign_in_ttl_minutes: int = 15
    records_session_ttl_hours: int = 12
    records_session_rate_limit: int = 20
    records_session_rate_window_seconds: float = 600

    # ---- Transaction intelligence engine: the /api/v1 API ----
    # Private storage for uploaded files and receipt PDFs. It must never be served directly.
    storage_dir: Path = BACKEND_DIR / "var" / "storage"
    # Uploaded files are deleted this many days after upload. Records keep the text read from them.
    upload_retention_days: int = 30
    max_text_bytes: int = 512 * 1024

    # Background jobs. By default a worker runs inside the API process; set this to false and run
    # `python -m app.engine.worker` to run it separately.
    engine_worker_in_process: bool = True
    engine_job_concurrency: int = 2
    engine_poll_seconds: float = 1.0
    engine_lease_seconds: float = 180.0
    engine_job_max_attempts: int = 4
    engine_retry_base_seconds: float = 15.0
    engine_retry_max_seconds: float = 900.0

    # What a person must check before approval. Medium-confidence money values are shown as inferred
    # by default; "needs_review" makes each one require confirmation.
    review_medium_confidence_financial: Literal["inferred", "needs_review"] = "inferred"
    review_block_possible_duplicates: bool = True
    # Text exports with dates like 03/04/2025 are read day-first unless the export proves otherwise.
    text_dates_day_first: bool = True

    # Integration events (for example to n8n). Without a URL they are recorded but not sent.
    webhook_url: str = ""
    webhook_secret: SecretStr | None = None
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = 8

    # /api/metrics is served only when this token is set (sent as a Bearer token).
    metrics_token: SecretStr | None = None

    # Per-account rate limits for the v1 API.
    v1_processing_rate_limit: int = 30
    v1_processing_rate_window_seconds: float = 600
    v1_upload_rate_limit: int = 200
    v1_upload_rate_window_seconds: float = 600
    v1_write_rate_limit: int = 600
    v1_write_rate_window_seconds: float = 600

    @property
    def webhooks_configured(self) -> bool:
        return bool(self.webhook_url and self.webhook_secret and self.webhook_secret.get_secret_value())

    @property
    def database_configured(self) -> bool:
        return bool(self.database_url and self.database_url.get_secret_value())

    @property
    def whatsapp_configured(self) -> bool:
        """Everything the channel needs, including the database that makes it reliable."""
        secrets = (self.whatsapp_access_token, self.whatsapp_app_secret, self.whatsapp_verify_token)
        return (
            self.database_configured
            and bool(self.whatsapp_phone_number_id)
            and all(secret is not None and secret.get_secret_value() for secret in secrets)
        )

    @property
    def web_url(self) -> str:
        return (self.public_web_url or (self.cors_origins[0] if self.cors_origins else "")).rstrip("/")

    @property
    def whatsapp_link_number(self) -> str:
        """ChatReceipt's number for wa.me links: digits only, or "" when it isn't set or isn't a number."""
        digits = "".join(character for character in self.whatsapp_display_number if character.isdigit())
        return digits if 7 <= len(digits) <= 15 else ""

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.frontend_origins.split(",") if origin.strip()]

    @property
    def max_request_bytes(self) -> int:
        """Upper bound for any request body: all files plus room for multipart framing."""
        return self.max_total_bytes + 1024 * 1024

    @property
    def extraction_configured(self) -> bool:
        """Whether Anthropic credentials are available."""
        return bool(
            self.anthropic_api_key
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        )

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    def prices_per_mtok(self, provider: str) -> tuple[float, float]:
        """Input and output prices (USD per million tokens) for cost estimates in logs."""
        if provider == "gemini":
            return self.gemini_input_price_per_mtok, self.gemini_output_price_per_mtok
        return self.extraction_input_price_per_mtok, self.extraction_output_price_per_mtok


@lru_cache
def get_settings() -> Settings:
    return Settings()
