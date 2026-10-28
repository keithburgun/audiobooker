"""Where Audiobooker keeps its models, voices, caches and settings.

Everything lives under %LOCALAPPDATA%\\Audiobooker on Windows (or
~/.local/share/audiobooker elsewhere) so nothing is written next to the
program files and uninstalling the app does not delete multi-GB models
unless the user asks for it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def data_root() -> Path:
    override = os.environ.get("AUDIOBOOKER_HOME")
    if override:
        root = Path(override)
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Audiobooker"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "audiobooker"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sub(name: str) -> Path:
    p = data_root() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def models_dir() -> Path:
    return _sub("models")


def voices_dir() -> Path:
    """User voice clips (WAV/MP3/FLAC) for the cloning engines."""
    return _sub("voices")


def jobs_dir() -> Path:
    """Per-chapter rendered audio, kept so interrupted books can resume."""
    return _sub("jobs")


def logs_dir() -> Path:
    return _sub("logs")


def settings_file() -> Path:
    return data_root() / "settings.json"


def configure_model_cache() -> None:
    """Point Hugging Face downloads at our own models folder.

    Must run before huggingface_hub / transformers are imported.
    """
    hf_home = models_dir() / "huggingface"
    os.environ.setdefault("HF_HOME", str(hf_home))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    # Torch hub (used by a few sub-models) also goes in our folder.
    os.environ.setdefault("TORCH_HOME", str(models_dir() / "torch"))
