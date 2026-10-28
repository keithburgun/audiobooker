from __future__ import annotations

import os

from .base import Engine, EngineOption, Voice, gpu_description, pick_device  # noqa: F401


def engine_classes() -> list[type[Engine]]:
    from .chatterbox_engine import ChatterboxEngine, ChatterboxTurboEngine
    from .kokoro_engine import KokoroEngine

    classes: list[type[Engine]] = [KokoroEngine, ChatterboxEngine, ChatterboxTurboEngine]
    if os.environ.get("AUDIOBOOKER_DEV"):
        from .test_engine import ToneEngine
        classes.append(ToneEngine)
    return classes


_instances: dict[str, Engine] = {}


def get_engine(key: str) -> Engine:
    """Return a (cached) engine instance. Only one engine stays loaded on the
    GPU at a time; switching unloads the previous one."""
    for cls in engine_classes():
        if cls.key == key:
            if key not in _instances:
                _instances[key] = cls()
            for k, inst in _instances.items():
                if k != key and inst.loaded:
                    inst.unload()
            return _instances[key]
    raise KeyError(key)
