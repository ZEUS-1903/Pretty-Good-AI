"""Environment-backed configuration.

Everything the bot needs is read from environment variables (loaded from .env).
No secrets ever land in code or in the artifacts directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val or ""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- Telephony -------------------------------------------------------
    twilio_account_sid: str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID", required=True))
    twilio_auth_token: str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN", required=True))
    twilio_from_number: str = field(default_factory=lambda: _env("TWILIO_FROM_NUMBER", required=True))

    # Hard-coded default so a typo can never dial a stranger. Override only
    # if you are pointing the harness at your own sandbox agent.
    target_number: str = field(default_factory=lambda: _env("TARGET_NUMBER", "+18054398008"))

    # --- Public tunnel ---------------------------------------------------
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", ""))
    use_ngrok: bool = field(default_factory=lambda: _env_bool("USE_NGROK", False))
    port: int = field(default_factory=lambda: _env_int("PORT", 8080))

    # --- OpenAI ----------------------------------------------------------
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", required=True))
    realtime_model: str = field(default_factory=lambda: _env("REALTIME_MODEL", "gpt-realtime-2.1"))
    realtime_voice: str = field(default_factory=lambda: _env("REALTIME_VOICE", "marin"))
    transcription_model: str = field(
        default_factory=lambda: _env("TRANSCRIPTION_MODEL", "gpt-4o-transcribe")
    )
    analysis_model: str = field(default_factory=lambda: _env("ANALYSIS_MODEL", "gpt-5.6-terra"))

    # --- Call behaviour --------------------------------------------------
    max_call_seconds: int = field(default_factory=lambda: _env_int("MAX_CALL_SECONDS", 240))
    hangup_grace_seconds: float = field(
        default_factory=lambda: float(_env("HANGUP_GRACE_SECONDS", "2.0"))
    )
    inter_call_seconds: int = field(default_factory=lambda: _env_int("INTER_CALL_SECONDS", 20))
    twilio_recording: bool = field(default_factory=lambda: _env_bool("TWILIO_RECORDING", True))

    # --- Output ----------------------------------------------------------
    artifacts_dir: Path = field(
        default_factory=lambda: Path(_env("ARTIFACTS_DIR", str(REPO_ROOT / "artifacts")))
    )

    def ws_url(self, path: str = "/ws/media") -> str:
        base = self.public_base_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return base + path


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return _settings
