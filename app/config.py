"""Runtime configuration, loaded from the environment (optionally via .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv is convenient but not required.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent

if load_dotenv is not None:
    load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """All knobs for one running instance."""

    postcode_api_key: str = field(default_factory=lambda: os.getenv("POSTCODE_API_KEY", "").strip())
    postcode_api_base: str = field(
        default_factory=lambda: os.getenv("POSTCODE_API_BASE", "https://api.postcode.gov.ng").rstrip("/")
    )
    max_level: int = field(default_factory=lambda: _int("POSTCODE_MAX_LEVEL", 3))

    risk_block_threshold: int = field(default_factory=lambda: _int("RISK_BLOCK_THRESHOLD", 70))
    risk_review_threshold: int = field(default_factory=lambda: _int("RISK_REVIEW_THRESHOLD", 40))

    port: int = field(default_factory=lambda: _int("PORT", 8000))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(ROOT / "guardpoint.db")))

    @property
    def live(self) -> bool:
        """True when a real API key is configured; otherwise we simulate."""
        return bool(self.postcode_api_key)

    @property
    def mode(self) -> str:
        return "live" if self.live else "simulated"

    def clamp_level(self, requested: int) -> int:
        """Never ask the gateway for more than the organization has been granted."""
        return max(1, min(requested, self.max_level, 5))


settings = Settings()
