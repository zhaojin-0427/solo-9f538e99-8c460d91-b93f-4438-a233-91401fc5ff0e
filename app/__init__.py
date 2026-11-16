"""Flask application factory."""
from __future__ import annotations

import os

from flask import Flask

from . import config
from .db import init_app
from .routes import bp
from .services import cleanup_staging


def _ensure_app_dirs(app: Flask) -> None:
    """Create storage directories based on the effective app configuration."""
    from pathlib import Path

    for key in ("STAGING_DIR", "PUBLISHED_DIR"):
        Path(app.config[key]).mkdir(parents=True, exist_ok=True)
    Path(app.config["STAGING_DIR"], "batches").mkdir(parents=True, exist_ok=True)
    Path(app.config["STAGING_DIR"], "uploads").mkdir(parents=True, exist_ok=True)
    Path(app.config["DB_PATH"]).parent.mkdir(parents=True, exist_ok=True)


def create_app(config_overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    # Flash messages need sessions; the key can be overridden for deployment.
    app.secret_key = os.environ.get("DICOM_SECRET_KEY", "dicom-gateway-dev-secret")
    if config_overrides:
        app.config.update(config_overrides)

    _ensure_app_dirs(app)
    init_app(app)

    with app.app_context():
        orphans = cleanup_staging()
    if orphans:
        app.logger.info("removed interrupted staging batches: %s", ", ".join(orphans))

    app.register_blueprint(bp)
    return app
