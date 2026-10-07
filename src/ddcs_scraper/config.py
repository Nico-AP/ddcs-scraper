import os
import socket
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

# Limits the server enforces (ddcs.metadata.scraper.serializers in ddcs-app).
MAX_CLAIM_LIMIT = 500
MAX_RESULTS_PER_REQUEST = 50
MAX_SCRAPER_ID_LENGTH = 100

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class ConfigError(Exception):
    """Raised when the configuration is missing or unusable."""


@dataclass(frozen=True)
class Settings:
    api_url: str
    api_token: str
    scraper_id: str
    claim_limit: int = 100
    submit_batch_size: int = 10
    idle_seconds: float = 300.0
    rate_delay: float = 1.0
    rate_jitter: float = 0.3
    caption_delay: float = 0.0
    fetch_captions: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        """Read the settings from the environment and a ``.env`` file.

        The ``.env`` file is looked for in the working directory and its
        parents. Variables already set in the environment win.
        """
        load_dotenv(find_dotenv(usecwd=True))

        api_url = os.environ.get("DDCS_API_URL", "").strip().rstrip("/")
        if not api_url.startswith(("http://", "https://")):
            msg = "DDCS_API_URL must be set to the base URL of the ddcs-app instance."
            raise ConfigError(msg)
        api_token = os.environ.get("DDCS_API_TOKEN", "").strip()
        if not api_token:
            msg = "DDCS_API_TOKEN must be set."
            raise ConfigError(msg)

        scraper_id = os.environ.get("DDCS_SCRAPER_ID", "").strip()
        return cls(
            api_url=api_url,
            api_token=api_token,
            scraper_id=(scraper_id or socket.gethostname())[:MAX_SCRAPER_ID_LENGTH],
            claim_limit=_int("DDCS_CLAIM_LIMIT", 100, 1, MAX_CLAIM_LIMIT),
            submit_batch_size=_int(
                "DDCS_SUBMIT_BATCH_SIZE", 10, 1, MAX_RESULTS_PER_REQUEST
            ),
            idle_seconds=_float("DDCS_IDLE_SECONDS", 300.0),
            rate_delay=_float("TIKTOK_RATE_DELAY", 1.0),
            rate_jitter=_float("TIKTOK_RATE_JITTER", 0.3, maximum=1.0),
            caption_delay=_float("TIKTOK_CAPTION_DELAY", 0.0),
            fetch_captions=_bool("TIKTOK_FETCH_CAPTIONS", default=True),
            log_level=os.environ.get("LOG_LEVEL", "").strip().upper() or "INFO",
        )


def _raw(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or not minimum <= value <= maximum:
        msg = f"{name} must be a whole number from {minimum} to {maximum}."
        raise ConfigError(msg)
    return value


def _float(name: str, default: float, maximum: float | None = None) -> float:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or value < 0 or (maximum is not None and value > maximum):
        upper = "" if maximum is None else f" and at most {maximum}"
        msg = f"{name} must be a number, at least 0{upper}."
        raise ConfigError(msg)
    return value


def _bool(name: str, *, default: bool) -> bool:
    raw = _raw(name)
    if raw is None:
        return default
    if raw.lower() in _TRUE:
        return True
    if raw.lower() in _FALSE:
        return False
    msg = f"{name} must be true or false."
    raise ConfigError(msg)
