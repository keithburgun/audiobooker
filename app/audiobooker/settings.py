from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from . import paths


@dataclass
class Settings:
    engine: str = "kokoro"
    voices: dict = field(default_factory=lambda: {"kokoro": "af_heart", "chatterbox": "kokoro:af_heart",
                                                   "chatterbox_turbo": "kokoro:af_heart"})
    speed: float = 1.0
    output_mode: str = "m4b"
    bitrate_kbps: int = 64
    out_dir: str = ""
    ocr: str = "auto"
    skip_footnotes: bool = True
    normalize: bool = True
    engine_opts: dict = field(default_factory=dict)  # {engine_key: {opt: value}}
    last_open_dir: str = ""
    geometry: str = ""

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(paths.settings_file().read_text("utf-8"))
            s = cls()
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            return s
        except Exception:
            return cls()

    def save(self) -> None:
        try:
            paths.settings_file().write_text(json.dumps(asdict(self), indent=2), "utf-8")
        except Exception:
            pass
