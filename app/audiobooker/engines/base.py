from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

ProgressFn = Callable[[float, str], None]


@dataclass
class Voice:
    id: str
    label: str
    group: str = ""
    recommended: bool = False


@dataclass
class EngineOption:
    """An extra slider an engine exposes in the UI."""
    key: str
    label: str
    minimum: float
    maximum: float
    default: float
    step: float = 0.05
    tooltip: str = ""


class Engine:
    key: str = ""
    name: str = ""
    description: str = ""
    sample_rate: int = 24000
    max_chunk_chars: int = 400
    native_speed: bool = True       # False -> speed is applied afterwards by time-stretching
    download_size: str = ""
    options: list[EngineOption] = []

    def __init__(self) -> None:
        self.device = "cpu"
        self.loaded = False

    # -- lifecycle ---------------------------------------------------------
    def voices(self) -> list[Voice]:
        raise NotImplementedError

    def is_downloaded(self) -> bool:
        raise NotImplementedError

    def load(self, progress: Optional[ProgressFn] = None, cancel=None) -> None:
        """Download (if needed) and load the model onto the GPU."""
        raise NotImplementedError

    def unload(self) -> None:
        self.loaded = False
        try:
            import gc
            import torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    # -- synthesis -----------------------------------------------------------
    def synth(self, text: str, voice_id: str, speed: float = 1.0, opts: Optional[dict] = None) -> np.ndarray:
        """Return mono float32 audio at self.sample_rate for one chunk of text."""
        raise NotImplementedError


def pick_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def gpu_description() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            return f"{p.name} ({p.total_memory / 2**30:.0f} GB)"
        return ""
    except Exception:
        return ""


def trim_silence(audio: np.ndarray, sr: int, threshold_db: float = -45.0, pad_ms: int = 40) -> np.ndarray:
    """Trim leading/trailing near-silence so pauses we insert are consistent."""
    if audio.size == 0:
        return audio
    thr = 10 ** (threshold_db / 20.0) * max(1e-6, float(np.max(np.abs(audio))))
    win = max(1, sr // 100)
    n = audio.size // win
    if n == 0:
        return audio
    env = np.abs(audio[: n * win]).reshape(n, win).max(axis=1)
    idx = np.nonzero(env > thr)[0]
    if idx.size == 0:
        return audio[:0]
    pad = int(sr * pad_ms / 1000)
    start = max(0, idx[0] * win - pad)
    end = min(audio.size, (idx[-1] + 1) * win + pad)
    return audio[start:end]
