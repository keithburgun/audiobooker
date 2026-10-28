from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

from .. import downloads, paths
from .base import Engine, EngineOption, Voice, pick_device, trim_silence

log = logging.getLogger(__name__)

AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}

# Kokoro voices offered as ready-made Chatterbox voices: we synthesise a short
# reference clip with Kokoro once and let Chatterbox clone it.
PRESET_FROM_KOKORO = [
    ("af_heart", "Heart (American female)"),
    ("af_bella", "Bella (American female)"),
    ("am_michael", "Michael (American male)"),
    ("am_fenrir", "Fenrir (American male, deep)"),
    ("bf_emma", "Emma (British female)"),
    ("bm_george", "George (British male)"),
    ("bm_fable", "Fable (British male)"),
]

_PRESET_TEXT = (
    "It was late in the evening when the letter finally arrived. She turned it over in her hands, "
    "studying the careful handwriting, and wondered who could have known where to find her. "
    "Outside, the rain had softened to a whisper against the glass."
)


def _ensure_watermarker() -> None:
    """resemble-perth silently sets PerthImplicitWatermarker = None when its
    optional imports fail (e.g. setuptools without pkg_resources), which makes
    Chatterbox crash on load. Fall back to the no-op watermarker in that case."""
    try:
        import perth
        if getattr(perth, "PerthImplicitWatermarker", None) is None:
            log.warning("Perth watermarker unavailable; using DummyWatermarker")
            perth.PerthImplicitWatermarker = perth.DummyWatermarker
    except Exception:
        log.exception("Could not import perth")


def user_voice_files() -> list[Path]:
    return sorted(p for p in paths.voices_dir().iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def preset_dir() -> Path:
    d = paths.voices_dir() / "_presets"
    d.mkdir(exist_ok=True)
    return d


class _ChatterboxBase(Engine):
    repo = ""
    files: list[str] = []
    sample_rate = 24000
    max_chunk_chars = 280
    native_speed = False

    def __init__(self) -> None:
        super().__init__()
        self._model = None
        self._cond_key: Optional[tuple] = None

    # -- voices --------------------------------------------------------------
    def voices(self) -> list[Voice]:
        v = [Voice("builtin", "Chatterbox default voice", "Built-in", True)]
        v += [Voice(f"kokoro:{k}", label, "Presets") for k, label in PRESET_FROM_KOKORO]
        v += [Voice(f"clip:{p.name}", f"{p.stem} (your clip)", "Your voice clips") for p in user_voice_files()]
        return v

    def _reference_path(self, voice_id: str, progress=None) -> Optional[str]:
        if voice_id == "builtin":
            return None
        if voice_id.startswith("clip:"):
            p = paths.voices_dir() / voice_id[5:]
            if not p.exists():
                raise FileNotFoundError(f"Voice clip not found: {p}")
            return str(p)
        if voice_id.startswith("kokoro:"):
            return str(self._ensure_preset(voice_id[7:], progress))
        raise ValueError(f"Unknown voice {voice_id}")

    def _ensure_preset(self, kokoro_voice: str, progress=None) -> Path:
        out = preset_dir() / f"{kokoro_voice}.wav"
        if out.exists():
            return out
        if progress:
            progress(0.0, "Creating preset voice clip with Kokoro (one time)…")
        from .kokoro_engine import KokoroEngine
        from ..audio import write_wav

        k = KokoroEngine()
        k.load(progress)
        try:
            audio = k.synth(_PRESET_TEXT, kokoro_voice, 1.0)
        finally:
            k.unload()
        write_wav(out, audio, k.sample_rate)
        return out

    # -- lifecycle -------------------------------------------------------------
    def is_downloaded(self) -> bool:
        return downloads.is_cached(self.repo, self.files)

    def _model_class(self):
        raise NotImplementedError

    def load(self, progress=None, cancel=None) -> None:
        if self.loaded:
            return
        snap = downloads.ensure_repo(self.repo, self.files, self.name.split(" —")[0] + " model", progress, cancel)
        if progress:
            progress(1.0, f"Loading {self.name.split(' —')[0]} onto the GPU…")
        self.device = pick_device()
        _ensure_watermarker()
        cls = self._model_class()
        self._model = cls.from_local(snap, self.device)
        self._cond_key = None
        self.loaded = True

    def unload(self) -> None:
        self._model = None
        self._cond_key = None
        super().unload()

    def prepare_voice(self, voice_id: str, opts: dict, progress=None) -> None:
        exaggeration = float(opts.get("exaggeration", 0.5))
        key = (voice_id, round(exaggeration, 3))
        if key == self._cond_key:
            return
        ref = self._reference_path(voice_id, progress)
        if ref is None:
            # Re-load the built-in conditionals shipped with the model.
            from chatterbox.tts import Conditionals
            snap = downloads.ensure_repo(self.repo, self.files, "model")
            self._model.conds = Conditionals.load(Path(snap) / "conds.pt", map_location="cpu").to(self.device)
        else:
            self._prepare(ref, exaggeration)
        self._cond_key = key

    def _prepare(self, ref: str, exaggeration: float) -> None:
        self._model.prepare_conditionals(ref, exaggeration=exaggeration)

    def _generate(self, text: str, opts: dict):
        raise NotImplementedError

    def synth(self, text: str, voice_id: str, speed: float = 1.0, opts: Optional[dict] = None) -> np.ndarray:
        import torch

        opts = opts or {}
        self.prepare_voice(voice_id, opts)
        expected = max(1.0, len(text) / 15.0)  # ~15 characters per second of speech
        best, best_err = None, None
        for attempt in range(3):
            with torch.inference_mode():
                wav = self._generate(text, opts)
            audio = wav.squeeze().detach().cpu().numpy().astype(np.float32)
            audio = trim_silence(audio, self.sample_rate)
            dur = audio.size / self.sample_rate
            err = abs(np.log(max(dur, 0.1) / expected))
            if best is None or err < best_err:
                best, best_err = audio, err
            # Autoregressive models occasionally ramble or cut off; retry those.
            if expected * 0.4 <= dur <= expected * 2.5 + 2:
                break
            log.info("Chatterbox output length looked wrong (%.1fs for %d chars); retrying", dur, len(text))
        return best


class ChatterboxEngine(_ChatterboxBase):
    key = "chatterbox"
    name = "Chatterbox — expressive (GPU)"
    description = ("Resemble AI's 0.5B model: the most natural, expressive narration here, and it can "
                   "clone a voice from a 10–20 second clip. Slower than Kokoro.")
    repo = "ResembleAI/chatterbox"
    files = ["ve.safetensors", "t3_cfg.safetensors", "s3gen.safetensors", "tokenizer.json", "conds.pt"]
    download_size = "≈3 GB"
    options = [
        EngineOption("exaggeration", "Expressiveness", 0.25, 1.0, 0.5, 0.05,
                     "Higher = more emotional, dramatic delivery. 0.5 suits most narration."),
        EngineOption("cfg_weight", "Pacing", 0.2, 0.8, 0.5, 0.05,
                     "Lower = slower, more deliberate delivery. Try 0.3 with high expressiveness."),
    ]

    def _model_class(self):
        from chatterbox.tts import ChatterboxTTS
        return ChatterboxTTS

    def _generate(self, text, opts):
        return self._model.generate(
            text,
            exaggeration=float(opts.get("exaggeration", 0.5)),
            cfg_weight=float(opts.get("cfg_weight", 0.5)),
        )


class ChatterboxTurboEngine(_ChatterboxBase):
    key = "chatterbox_turbo"
    name = "Chatterbox Turbo — expressive, faster (GPU)"
    description = ("A distilled 350M Chatterbox: similar voice quality and cloning, several times faster, "
                   "fewer controls.")
    repo = "ResembleAI/chatterbox-turbo"
    files = ["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"]
    download_size = "≈1.5 GB"
    options = []

    def _model_class(self):
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        return ChatterboxTurboTTS

    def _prepare(self, ref: str, exaggeration: float) -> None:
        self._model.prepare_conditionals(ref, exaggeration=0.0)

    def prepare_voice(self, voice_id: str, opts: dict, progress=None) -> None:
        if voice_id == "builtin":
            key = (voice_id, 0.0)
            if key == self._cond_key:
                return
            from chatterbox.tts_turbo import Conditionals
            snap = downloads.ensure_repo(self.repo, self.files, "model")
            self._model.conds = Conditionals.load(Path(snap) / "conds.pt", map_location="cpu").to(self.device)
            self._cond_key = key
            return
        super().prepare_voice(voice_id, {"exaggeration": 0.0}, progress)

    def _generate(self, text, opts):
        return self._model.generate(text)
