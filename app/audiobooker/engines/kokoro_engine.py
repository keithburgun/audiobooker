from __future__ import annotations

from typing import Optional

import numpy as np

from .. import downloads
from .base import Engine, Voice, pick_device

REPO = "hexgrad/Kokoro-82M"
FILES = ["config.json", "kokoro-v1_0.pth", "voices/af_*.pt", "voices/am_*.pt", "voices/bf_*.pt", "voices/bm_*.pt"]

# (id, label, recommended). Quality notes follow the Kokoro model card grades.
_VOICES = [
    ("af_heart", "Heart — American female, warm", True),
    ("af_bella", "Bella — American female, bright", True),
    ("af_nicole", "Nicole — American female, soft & close", False),
    ("af_aoede", "Aoede — American female", False),
    ("af_kore", "Kore — American female", False),
    ("af_sarah", "Sarah — American female", False),
    ("af_nova", "Nova — American female", False),
    ("af_sky", "Sky — American female", False),
    ("am_michael", "Michael — American male", True),
    ("am_fenrir", "Fenrir — American male, deep", True),
    ("am_puck", "Puck — American male, lively", False),
    ("am_echo", "Echo — American male", False),
    ("am_eric", "Eric — American male", False),
    ("am_liam", "Liam — American male", False),
    ("am_onyx", "Onyx — American male", False),
    ("bf_emma", "Emma — British female", True),
    ("bf_isabella", "Isabella — British female", False),
    ("bf_alice", "Alice — British female", False),
    ("bf_lily", "Lily — British female", False),
    ("bm_george", "George — British male", True),
    ("bm_fable", "Fable — British male", True),
    ("bm_lewis", "Lewis — British male", False),
    ("bm_daniel", "Daniel — British male", False),
]


class KokoroEngine(Engine):
    key = "kokoro"
    name = "Kokoro — fast, many voices"
    description = ("Small (82M) and very fast: a whole novel takes minutes on a GPU. "
                   "Clean, consistent narration with 23 English voices.")
    sample_rate = 24000
    max_chunk_chars = 350
    native_speed = True
    download_size = "≈340 MB"

    def __init__(self) -> None:
        super().__init__()
        self._model = None
        self._pipelines: dict[str, object] = {}
        self._snap = None

    def voices(self) -> list[Voice]:
        return [
            Voice(v, label + ("  ★" if rec else ""), "American" if v[0] == "a" else "British", rec)
            for v, label, rec in _VOICES
        ]

    def is_downloaded(self) -> bool:
        return downloads.is_cached(REPO, FILES)

    def load(self, progress=None, cancel=None) -> None:
        if self.loaded:
            return
        snap = downloads.ensure_repo(REPO, FILES, "Kokoro voice model", progress, cancel)
        if progress:
            progress(1.0, "Loading Kokoro…")
        from pathlib import Path
        from kokoro import KModel

        self.device = pick_device()
        self._snap = Path(snap)
        self._model = KModel(
            repo_id=REPO,
            config=str(Path(snap) / "config.json"),
            model=str(Path(snap) / "kokoro-v1_0.pth"),
        ).to(self.device).eval()
        self._pipelines = {}
        self.loaded = True

    def _pipeline(self, lang: str):
        if lang not in self._pipelines:
            from kokoro import KPipeline
            self._pipelines[lang] = KPipeline(lang_code=lang, repo_id=REPO, model=self._model)
        return self._pipelines[lang]

    def unload(self) -> None:
        self._model = None
        self._pipelines = {}
        super().unload()

    def synth(self, text: str, voice_id: str, speed: float = 1.0, opts: Optional[dict] = None) -> np.ndarray:
        import torch

        pipe = self._pipeline(voice_id[0] if voice_id[0] in "ab" else "a")
        parts = []
        with torch.inference_mode():
            voice = str(self._snap / "voices" / f"{voice_id}.pt") if self._snap else voice_id
            for result in pipe(text, voice=voice, speed=speed, split_pattern=None):
                audio = result.audio
                if audio is None:
                    continue
                parts.append(audio.detach().cpu().numpy().astype(np.float32).reshape(-1))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
