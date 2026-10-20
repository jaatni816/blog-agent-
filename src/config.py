"""Load, validate and log all runtime configuration.

Rules for this project:
  * Every secret comes from an environment variable (or a local .env file).
  * Secrets are never printed. `Config.safe_summary()` masks them.
  * If something is missing we fail fast and tell the user exactly what to fix.

Run `python -m src.main` from the project root.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Mapping
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Paths and constants
# --------------------------------------------------------------------------- #

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
ENV_FILE: Final[Path] = PROJECT_ROOT / ".env"
OUTPUT_DIR: Final[Path] = PROJECT_ROOT / "output"

# India Standard Time (UTC+05:30, no daylight saving).
IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

GOOGLE_TOKEN_URI: Final[str] = "https://oauth2.googleapis.com/token"
BLOGGER_API_NAME: Final[str] = "blogger"
BLOGGER_API_VERSION: Final[str] = "v3"
BLOGGER_SCOPE: Final[str] = "https://www.googleapis.com/auth/blogger"

# NOTE: Groq shut down `llama-3.3-70b-versatile` on 2026-08-16 for free and
# developer tiers. Their deprecation page recommends `openai/gpt-oss-120b`
# (or `qwen/qwen3.6-27b`), so that is our default. Override with GROQ_MODEL.
DEFAULT_GROQ_MODEL: Final[str] = "openai/gpt-oss-120b"
DEFAULT_LANGUAGE: Final[str] = "English"
DEFAULT_MAX_TOKENS: Final[int] = 8192
DEFAULT_RECENT_TITLES: Final[int] = 50
DEFAULT_IMAGE_COUNT: Final[int] = 3
DEFAULT_TIMEOUT_SECONDS: Final[int] = 60
DEFAULT_MAX_ATTEMPTS: Final[int] = 3

_TRUE_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on"})

# Required keys -> where the user gets them.
REQUIRED_KEYS: Final[Mapping[str, str]] = {
    "GROQ_API_KEY": "https://console.groq.com/keys -> Create API Key",
    "PIXABAY_API_KEY": "https://pixabay.com/api/docs/ -> 'Get a free API key'",
    "GOOGLE_CLIENT_ID": "Google Cloud console -> OAuth client ID (Desktop app)",
    "GOOGLE_CLIENT_SECRET": "Google Cloud console -> OAuth client secret (Desktop app)",
    "GOOGLE_REFRESH_TOKEN": "Run `python scripts/get_refresh_token.py` once, locally",
    "BLOG_ID": "https://www.googleapis.com/blogger/v3/users/self/blogs -> the long id",
    "BLOG_NICHE": "Your blog topic, for example: Technology and AI tools for beginners",
}

# Optional keys -> default value.
OPTIONAL_KEYS: Final[Mapping[str, str]] = {
    "GROQ_MODEL": DEFAULT_GROQ_MODEL,
    "BLOG_LANGUAGE": DEFAULT_LANGUAGE,
    "PUBLISH_AS_DRAFT": "true",
    "GROQ_MAX_TOKENS": str(DEFAULT_MAX_TOKENS),
    "BLOG_IMAGE_COUNT": str(DEFAULT_IMAGE_COUNT),
    "RECENT_TITLES_LIMIT": str(DEFAULT_RECENT_TITLES),
    "HTTP_TIMEOUT_SECONDS": str(DEFAULT_TIMEOUT_SECONDS),
    "MAX_ATTEMPTS": str(DEFAULT_MAX_ATTEMPTS),
    "LOG_LEVEL": "INFO",
}

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


class _IstFormatter(logging.Formatter):
    """Formatter that prints timestamps in IST instead of machine local time."""

    default_time_format = "%Y-%m-%d %H:%M:%S IST"

    def formatTime(  # noqa: N802 - name required by logging API
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        moment = datetime.fromtimestamp(record.created, tz=IST)
        return moment.strftime(datefmt or self.default_time_format)


def _force_utf8_console() -> None:
    """Make the console UTF-8 so smart quotes and dashes never crash a run.

    Windows consoles default to cp1252, which raises UnicodeEncodeError on
    characters such as the non-breaking hyphen or an em dash. Generated
    articles contain those, so we relax the encoding instead of failing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # already detached or not a text stream
            log.debug("Could not reconfigure %s to UTF-8.", stream, exc_info=True)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, with timestamps and IST."""
    _force_utf8_console()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        _IstFormatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
        )
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # Third-party libraries are extremely chatty at INFO level.
    for noisy in ("googleapiclient", "urllib3", "requests", "oauth2client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Config object
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Config:
    """Validated configuration for one run of the agent."""

    # Secrets
    groq_api_key: str
    pixabay_api_key: str
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str

    # Content
    blog_id: str
    niche: str
    language: str = DEFAULT_LANGUAGE
    groq_model: str = DEFAULT_GROQ_MODEL
    groq_max_tokens: int = DEFAULT_MAX_TOKENS

    # Behaviour
    publish_as_draft: bool = True
    image_count: int = DEFAULT_IMAGE_COUNT
    recent_titles_limit: int = DEFAULT_RECENT_TITLES
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    # -- helpers ------------------------------------------------------------ #

    @property
    def today_ist(self) -> str:
        """Today's date in IST as YYYY-MM-DD (used for the daily check)."""
        return datetime.now(tz=IST).date().isoformat()

    def safe_summary(self) -> dict[str, object]:
        """Return a log-safe view of the config with secrets masked."""
        return {
            "blog_id": self.blog_id,
            "niche": self.niche,
            "language": self.language,
            "groq_model": self.groq_model,
            "groq_max_tokens": self.groq_max_tokens,
            "publish_as_draft": self.publish_as_draft,
            "image_count": self.image_count,
            "recent_titles_limit": self.recent_titles_limit,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "groq_api_key": _mask(self.groq_api_key),
            "pixabay_api_key": _mask(self.pixabay_api_key),
            "google_client_id": _mask(self.google_client_id),
            "google_refresh_token": _mask(self.google_refresh_token),
        }


def _mask(value: str, keep: int = 4) -> str:
    """Mask a secret so it can safely appear in logs."""
    if not value:
        return "<not set>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-2:]} ({len(value)} chars)"


def _as_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    return raw.lower() in _TRUE_VALUES


def _as_int(name: str, default: int, minimum: int = 1, maximum: int = 10_000) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}.")
    return value


def missing_required_keys() -> list[str]:
    """Return the names of required environment variables that are not set."""
    return [key for key in REQUIRED_KEYS if not (os.getenv(key) or "").strip()]


def _load_env_file(env_file: Path = ENV_FILE) -> None:
    """Load .env into the environment (real environment always wins)."""
    if env_file.exists():
        load_dotenv(env_file, override=False)
        log.debug("Loaded environment file: %s", env_file)
    else:
        log.debug(
            "No %s found - relying on process environment variables only.",
            env_file.name,
        )


def load_config(env_file: Path = ENV_FILE) -> Config:
    """Load and validate the configuration or raise `ConfigError`."""
    _load_env_file(env_file)

    missing = missing_required_keys()
    if missing:
        lines = [
            "Missing required configuration. Add these to your .env file or "
            f"environment variables ({ENV_FILE}):",
            "",
        ]
        for index, key in enumerate(missing, start=1):
            lines.append(f"  {index}. {key} - {REQUIRED_KEYS[key]}")
        lines += [
            "",
            "Start from the template:  cp .env.example .env",
        ]
        raise ConfigError("\n".join(lines))

    config = Config(
        groq_api_key=(os.environ["GROQ_API_KEY"] or "").strip(),
        pixabay_api_key=(os.environ["PIXABAY_API_KEY"] or "").strip(),
        google_client_id=(os.environ["GOOGLE_CLIENT_ID"] or "").strip(),
        google_client_secret=(os.environ["GOOGLE_CLIENT_SECRET"] or "").strip(),
        google_refresh_token=(os.environ["GOOGLE_REFRESH_TOKEN"] or "").strip(),
        blog_id=(os.environ["BLOG_ID"] or "").strip(),
        niche=(os.environ["BLOG_NICHE"] or "").strip(),
        language=(os.getenv("BLOG_LANGUAGE") or DEFAULT_LANGUAGE).strip(),
        groq_model=(os.getenv("GROQ_MODEL") or DEFAULT_GROQ_MODEL).strip(),
        groq_max_tokens=_as_int(
            "GROQ_MAX_TOKENS", DEFAULT_MAX_TOKENS, 512, 32_768
        ),
        publish_as_draft=_as_bool("PUBLISH_AS_DRAFT", True),
        image_count=_as_int("BLOG_IMAGE_COUNT", DEFAULT_IMAGE_COUNT, 1, 10),
        recent_titles_limit=_as_int(
            "RECENT_TITLES_LIMIT", DEFAULT_RECENT_TITLES, 1, 500
        ),
        timeout_seconds=_as_int(
            "HTTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 5, 600
        ),
        max_attempts=_as_int("MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, 1, 10),
    )

    if not config.google_client_id.endswith(".apps.googleusercontent.com"):
        log.warning(
            "GOOGLE_CLIENT_ID does not look like a Desktop-app client id "
            "(it normally ends with apps.googleusercontent.com)."
        )
    if len(config.blog_id) < 5:
        raise ConfigError(
            f"BLOG_ID looks wrong ({config.blog_id!r}). It should be a long number."
        )

    log.debug("Configuration loaded: %s", config.safe_summary())
    return config