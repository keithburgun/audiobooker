"""Model downloads from Hugging Face with progress reporting.

Downloads go into Audiobooker's own models folder (see paths.configure_model_cache).
"""
from __future__ import annotations

import fnmatch
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

ProgressFn = Callable[[float, str], None]


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _marker(repo_id: str, patterns: list[str]) -> Path:
    import hashlib
    from . import paths
    key = hashlib.sha1(("|".join(sorted(patterns))).encode()).hexdigest()[:10]
    d = paths.models_dir() / "complete"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{repo_id.replace('/', '__')}__{key}.json"


def is_cached(repo_id: str, patterns: list[str]) -> bool:
    """True if a previous download of these files finished and they are all
    still on disk."""
    import json
    m = _marker(repo_id, patterns)
    try:
        files = json.loads(m.read_text("utf-8"))["files"]
    except Exception:
        return False
    return bool(files) and all(_local_file(repo_id, f) is not None for f in files)


def _local_file(repo_id: str, filename: str) -> Optional[str]:
    from huggingface_hub import try_to_load_from_cache

    r = try_to_load_from_cache(repo_id, filename)
    return r if isinstance(r, str) else None


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def ensure_repo(repo_id: str, patterns: list[str], label: str, progress: Optional[ProgressFn] = None,
                cancel: Optional[Callable[[], bool]] = None) -> str:
    """Download the files of `repo_id` matching `patterns` (if needed) and
    return the local snapshot directory."""
    from huggingface_hub import snapshot_download

    progress = progress or (lambda f, m: None)
    if is_cached(repo_id, patterns):
        return snapshot_download(repo_id, allow_patterns=patterns, local_files_only=True)
    return _download(repo_id, patterns, label, progress, cancel)


def _download(repo_id, patterns, label, progress, cancel) -> str:
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download
    from huggingface_hub import constants

    progress(0.0, f"Downloading {label}…")
    info = HfApi().model_info(repo_id, files_metadata=True)
    files = [(s.rfilename, s.size or 0) for s in info.siblings if _matches(s.rfilename, patterns)]
    if not files:
        raise RuntimeError(f"No model files matching {patterns} found in {repo_id}")
    total = sum(sz for _, sz in files) or 1
    done = 0

    # Watch the partially-downloaded blob files for live byte counts.
    blobs = Path(constants.HF_HUB_CACHE) / ("models--" + repo_id.replace("/", "--")) / "blobs"
    stop = threading.Event()
    current = {"name": "", "size": 0}

    def watch():
        while not stop.wait(0.5):
            partial = 0
            try:
                for p in blobs.glob("*.incomplete"):
                    partial += p.stat().st_size
            except Exception:
                pass
            frac = min(0.999, (done + partial) / total)
            progress(frac, f"Downloading {label}: {current['name']} — "
                           f"{_human(done + partial)} of {_human(total)}")

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        for name, size in files:
            if cancel and cancel():
                raise RuntimeError("Download cancelled")
            current["name"], current["size"] = name.split("/")[-1], size
            if _local_file(repo_id, name) is None:
                for attempt in range(3):
                    try:
                        hf_hub_download(repo_id, name)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        time.sleep(2 + attempt * 3)
            done += size
    finally:
        stop.set()
        t.join(timeout=2)
    import json
    _marker(repo_id, patterns).write_text(json.dumps({"files": [n for n, _ in files]}), "utf-8")
    progress(1.0, f"{label} downloaded")
    return snapshot_download(repo_id, allow_patterns=patterns, local_files_only=True)
