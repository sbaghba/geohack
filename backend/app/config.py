"""Settings from environment variables (and backend/.env when present)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

try:  # optional: load backend/.env for local dev
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env")
except ImportError:
    pass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # Gemini
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY") or None
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    gemini_fallback_model: str = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")

    # Data
    data_dir: Path = Path(os.getenv("DATA_DIR", str(BACKEND_DIR / "data")))
    use_mock: bool = _bool("USE_MOCK", True)  # M1: everything served from the contract mocks

    # HTTP
    allowed_origins: list[str] = field(default_factory=lambda: _list(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5500,http://127.0.0.1:5500,http://localhost:8080",
    ))
    allow_origin_regex: str | None = os.getenv(
        "ALLOW_ORIGIN_REGEX", r"https://.*\.(github\.io|vercel\.app|trycloudflare\.com)"
    ) or None

    @property
    def grid_path(self) -> Path:
        return self.data_dir / "grid" / "nc_h3r7.parquet"


settings = Settings()
