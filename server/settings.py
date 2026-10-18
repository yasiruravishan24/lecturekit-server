"""Configuration from environment variables (SERVER_PLAN.md section 11). Secrets only come from the environment."""
import os
from dataclasses import dataclass
from pathlib import Path

WEAK_SECRETS = {"", "change-me", "changeme", "secret", "dev", "test"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number") from None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    jwt_secret: str
    api_prefix: str = "/api"                 # all routes live under this prefix; "/" serves the web app
    daily_limit: int = 5
    max_recording_mb: int = 1500
    max_chunk_mb: int = 60
    max_slides_mb: int = 100
    keep_media_days: int = 7
    web_origin: str = ""
    extension_origin: str = ""
    token_days: int = 7
    max_chunks_per_session: int = 200
    login_max_failures: int = 5
    login_window_seconds: int = 600
    bcrypt_rounds: int = 12
    preload_whisper: bool = True
    housekeeping_seconds: int = 3600
    log_retention_days: int = 14
    web_dist: Path | None = None
    smtp_host: str = ""                      # empty: emails are written to the server log instead of sent
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    google_client_id: str = ""               # empty: Google login is switched off
    otp_minutes: int = 10
    otp_max_attempts: int = 5
    resend_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        web_dist = os.getenv("WEB_DIST")
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "/data")),
            jwt_secret=os.getenv("JWT_SECRET", ""),
            api_prefix=os.getenv("API_PREFIX", "/api").rstrip("/"),
            daily_limit=_int("DAILY_LIMIT", 5),
            max_recording_mb=_int("MAX_RECORDING_MB", 1500),
            max_chunk_mb=_int("MAX_CHUNK_MB", 60),
            max_slides_mb=_int("MAX_SLIDES_MB", 100),
            keep_media_days=_int("KEEP_MEDIA_DAYS", 7),
            web_origin=os.getenv("WEB_ORIGIN", ""),
            extension_origin=os.getenv("EXTENSION_ORIGIN", ""),
            log_retention_days=_int("LOG_RETENTION_DAYS", 14),
            web_dist=Path(web_dist) if web_dist else None,
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_from=os.getenv("SMTP_FROM", ""),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
        )

    def validate(self) -> None:
        """Refuse to start with a missing or weak secret (SERVER_PLAN.md section 9)."""
        if self.jwt_secret.lower() in WEAK_SECRETS or len(self.jwt_secret) < 32:
            raise RuntimeError("JWT_SECRET must be set to at least 32 random characters")

    # derived paths
    @property
    def db_path(self) -> Path:
        return self.data_dir / "server.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"

    @property
    def cors_origins(self) -> list[str]:
        return [o for o in (self.web_origin, self.extension_origin) if o]
