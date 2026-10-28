"""A stand-in engine for development and tests (enabled with AUDIOBOOKER_DEV=1).

It produces a soft tone whose length tracks the text, so the whole pipeline
(chunking, pauses, encoding, chapters, resume) can be exercised without
downloading a real model.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from .base import Engine, Voice


class ToneEngine(Engine):
    key = "test_tone"
    name = "Test tones (developer)"
    description = "Plays tones instead of speech. For testing only."
    sample_rate = 24000
    max_chunk_chars = 300
    native_speed = True

    def voices(self) -> list[Voice]:
        return [Voice("low", "Low tone", "Test", True), Voice("high", "High tone", "Test")]

    def is_downloaded(self) -> bool:
        return True

    def load(self, progress=None, cancel=None) -> None:
        self.loaded = True

    def synth(self, text: str, voice_id: str, speed: float = 1.0, opts: Optional[dict] = None) -> np.ndarray:
        secs = max(0.3, len(text) / 15.0 / speed)
        t = np.arange(int(secs * self.sample_rate)) / self.sample_rate
        f = 220.0 if voice_id == "low" else 440.0
        env = np.minimum(1.0, np.minimum(t, secs - t) * 20)
        return (0.2 * np.sin(2 * np.pi * f * t) * env).astype(np.float32)
