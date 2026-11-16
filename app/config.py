"""Application configuration.

All paths can be overridden through environment variables so the same image
works both with the provided Docker Compose volume and in local test runs.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = _env_path("DICOM_DATA_DIR", BASE_DIR / "storage")
STAGING_DIR = _env_path("DICOM_STAGING_DIR", DATA_DIR / "staging")
PUBLISHED_DIR = _env_path("DICOM_PUBLISHED_DIR", DATA_DIR / "published")
DB_PATH = _env_path("DICOM_DB_PATH", DATA_DIR / "registry.db")

# Business limits from the specification.
MAX_FILES_PER_BATCH = 10
# Hard cap for a single uploaded file (500 MB).
MAX_CONTENT_LENGTH = int(os.environ.get("DICOM_MAX_UPLOAD_BYTES", 500 * 1024 * 1024))

# Hospital code: upper/lower letters, digits, dash and underscore, 2..20 chars.
HOSPITAL_CODE_REGEX = r"^[A-Za-z0-9_-]{2,20}$"

# Uncompressed CT transfer syntaxes accepted by the service.
UNCOMPRESSED_TRANSFER_SYNTAXES = frozenset(
    {
        "1.2.840.10008.1.2",      # Implicit VR Little Endian
        "1.2.840.10008.1.2.1",    # Explicit VR Little Endian
        "1.2.840.10008.1.2.2",    # Explicit VR Big Endian (retired, still uncompressed)
    }
)


def ensure_dirs() -> None:
    """Create storage directories if missing."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
