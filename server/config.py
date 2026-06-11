"""Env-driven settings.

Everything deployment-specific arrives via environment variables so the
same image runs on Railway, docker-compose, or bare metal. Inference is
reached only through VISION_BASE_URL — no outbound network anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    # SQLite + per-batch session media live under here.
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("DATA_DIR", "data"))
    )
    # OpenAI-compatible endpoint of the self-hosted vision sidecar
    # (llama-server). Empty disables Tier B escalation.
    vision_base_url: str = field(
        default_factory=lambda: os.environ.get("VISION_BASE_URL", "")
    )
    vision_model: str = field(
        default_factory=lambda: os.environ.get("VISION_MODEL", "qwen3-vl-4b")
    )
    # Bounded Tier B concurrency: the CPU model is slow, so escalations
    # queue behind a small worker pool while Tier A keeps streaming.
    # Default matches the reference sidecar's single slot (--parallel 1);
    # extra workers just stack in the server's queue and burn the HTTP
    # timeout waiting. Raise only for runtimes that decode in parallel.
    vision_workers: int = field(
        default_factory=lambda: int(os.environ.get("VISION_WORKERS", "1"))
    )
    ocr_workers: int = field(
        default_factory=lambda: int(os.environ.get("OCR_WORKERS", "4"))
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cola_proof.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


settings = Settings()
