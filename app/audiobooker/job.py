"""Turns a Book into audio files.

Each chapter is rendered to a FLAC file in the job cache, named by a hash of
(engine, voice, settings, text). Re-running the same book with the same
settings skips chapters that are already done, so an interrupted 10-hour book
resumes where it stopped, and re-exporting in another format is instant.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import audio, paths, textprep
from .engines import Engine
from .pdf_extract import Book, Chapter

log = logging.getLogger(__name__)

PAUSE_AFTER_HEADING = 1.1
PAUSE_PARAGRAPH = 0.55
PAUSE_SENTENCE_CHUNK = 0.12
LEAD_IN = 0.6
TAIL = 1.6


class Cancelled(Exception):
    pass


@dataclass
class JobSettings:
    engine: str = "kokoro"
    voice: str = "af_heart"
    speed: float = 1.0
    output_mode: str = "m4b"  # "mp3_chapters" | "mp3_single" | "m4b"
    bitrate_kbps: int = 64
    out_dir: str = ""
    normalize: bool = True
    engine_opts: dict = field(default_factory=dict)


@dataclass
class Progress:
    fraction: float
    message: str
    chapter_index: int = -1
    chars_done: int = 0
    chars_total: int = 0


def _chapter_key(ch: Chapter, s: JobSettings) -> str:
    h = hashlib.sha1()
    voice_id = s.voice
    if s.voice.startswith("clip:"):  # a replaced clip must not reuse old audio
        clip = paths.voices_dir() / s.voice[5:]
        if clip.exists():
            st = clip.stat()
            voice_id = f"{s.voice}|{st.st_size}|{int(st.st_mtime)}"
    h.update(json.dumps([s.engine, voice_id, round(s.speed, 3), sorted(s.engine_opts.items()), 2]).encode())
    for p in ch.paragraphs:
        h.update(p.kind.encode())
        h.update(p.text.encode("utf-8"))
    return h.hexdigest()[:20]


def book_cache_dir(book: Book) -> Path:
    h = hashlib.sha1(Path(book.path).name.encode("utf-8") + str(book.page_count).encode()).hexdigest()[:12]
    d = paths.jobs_dir() / h
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_size_bytes() -> int:
    return sum(p.stat().st_size for p in paths.jobs_dir().rglob("*") if p.is_file())


def clear_cache() -> None:
    import shutil
    for p in paths.jobs_dir().iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def render_chapter_audio(engine: Engine, ch: Chapter, s: JobSettings, writer: audio.ChapterWriter,
                         on_chars: Callable[[int], None], cancel: Callable[[], bool]) -> None:
    sr = engine.sample_rate
    speed_native = s.speed if engine.native_speed else 1.0
    writer.write(audio.silence(LEAD_IN, sr))
    for pi, para in enumerate(ch.paragraphs):
        text = textprep.normalize(para.text)
        if not text:
            continue
        pieces = textprep.chunk(text, engine.max_chunk_chars)
        for ci, piece in enumerate(pieces):
            if cancel():
                raise Cancelled()
            wav = engine.synth(piece, s.voice, speed_native, s.engine_opts)
            writer.write(wav)
            if ci < len(pieces) - 1:
                writer.write(audio.silence(PAUSE_SENTENCE_CHUNK, sr))
            on_chars(len(piece))
        pause = PAUSE_AFTER_HEADING if para.kind == "heading" else PAUSE_PARAGRAPH
        writer.write(audio.silence(pause, sr))
    writer.write(audio.silence(TAIL, sr))


def run_job(
    book: Book,
    s: JobSettings,
    engine: Engine,
    progress: Callable[[Progress], None],
    cancel: Callable[[], bool],
    meta: Optional[audio.BookMeta] = None,
) -> list[Path]:
    chapters = [c for c in book.chapters if c.include]
    if not chapters:
        raise ValueError("No chapters are selected.")
    cache = book_cache_dir(book)
    total_chars = sum(len(c.text) for c in chapters) or 1

    # --- 1. load model -------------------------------------------------------
    if not engine.loaded:
        engine.load(lambda f, m: progress(Progress(0.0, m, -1, 0, total_chars)), cancel)
    # Voice preparation can involve a one-off download/preset clip.
    if hasattr(engine, "prepare_voice"):
        engine.prepare_voice(s.voice, s.engine_opts,
                             lambda f, m: progress(Progress(0.0, m, -1, 0, total_chars)))

    # --- 2. synthesise each chapter -----------------------------------------
    rendered: list[audio.RenderedChapter] = []
    chars_done = 0
    tempo = 1.0 if engine.native_speed else s.speed
    synth_weight = 0.93  # share of the progress bar for synthesis vs. encoding
    for idx, ch in enumerate(chapters):
        key = _chapter_key(ch, s)
        flac = cache / f"{key}.flac"
        info = cache / f"{key}.json"
        if flac.exists() and info.exists():
            dur = json.loads(info.read_text())["duration"]
            chars_done += len(ch.text)
            rendered.append(audio.RenderedChapter(ch.title, flac, dur))
            progress(Progress(synth_weight * chars_done / total_chars,
                              f"Chapter {idx + 1} of {len(chapters)} already rendered — skipping",
                              idx, chars_done, total_chars))
            continue

        writer = audio.ChapterWriter(flac, engine.sample_rate, tempo)
        start_chars = chars_done
        t0 = time.time()

        def on_chars(n: int, idx=idx, ch=ch):
            nonlocal chars_done
            chars_done += n
            progress(Progress(synth_weight * chars_done / total_chars,
                              f"Narrating chapter {idx + 1} of {len(chapters)}: {ch.title}",
                              idx, chars_done, total_chars))

        try:
            render_chapter_audio(engine, ch, s, writer, on_chars, cancel)
            dur = writer.close()
        except BaseException:
            writer.abort()
            raise
        chars_done = start_chars + len(ch.text)
        info.write_text(json.dumps({"duration": dur, "title": ch.title, "settings": asdict(s)}))
        log.info("Chapter %d rendered: %.0fs audio in %.0fs", idx + 1, dur, time.time() - t0)
        rendered.append(audio.RenderedChapter(ch.title, flac, dur))

    # --- 3. encode ---------------------------------------------------------
    meta = meta or audio.BookMeta(book.title, book.author, book.cover_png)
    out_dir = Path(s.out_dir) if s.out_dir else Path(book.path).parent

    def enc_progress(f: float, m: str):
        progress(Progress(synth_weight + (1 - synth_weight) * f, m, -1, total_chars, total_chars))

    outputs = audio.export(rendered, meta, out_dir, s.output_mode, s.bitrate_kbps, s.normalize,
                           enc_progress, cancel)
    progress(Progress(1.0, "Finished", -1, total_chars, total_chars))
    return outputs
