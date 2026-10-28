"""Synthesise one sentence with each named engine and check the result.

Usage: python tests/engine_smoke.py kokoro chatterbox_turbo chatterbox
(run with app/ on PYTHONPATH)
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from audiobooker import paths  # noqa: E402

paths.configure_model_cache()

from audiobooker import audio  # noqa: E402
from audiobooker.engines import get_engine  # noqa: E402

TEXT = "The lighthouse stood at the edge of the world, and the wind howled through its iron railings."
out_dir = Path("engine_smoke")
out_dir.mkdir(exist_ok=True)
failed = False
for key in sys.argv[1:]:
    eng = get_engine(key)
    voices = [v.id for v in eng.voices()]
    voice = "kokoro:bm_george" if key.startswith("chatterbox") else voices[0]
    t0 = time.time()
    eng.load(lambda f, m: print(f"  {m}", flush=True))
    if hasattr(eng, "prepare_voice"):
        eng.prepare_voice(voice, {}, lambda f, m: print(f"  {m}", flush=True))
    wav = eng.synth(TEXT, voice, 1.0, {})
    dur = wav.size / eng.sample_rate
    audio.write_wav(out_dir / f"{key}.wav", wav, eng.sample_rate)
    ok = 2.5 < dur < 15 and float(abs(wav).max()) > 0.01
    print(f"{key}: voice={voice} {dur:.1f}s of audio in {time.time() - t0:.0f}s -> {'OK' if ok else 'BAD'}")
    failed |= not ok
    eng.unload()
sys.exit(1 if failed else 0)
