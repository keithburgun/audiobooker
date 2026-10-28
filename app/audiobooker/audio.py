"""Audio encoding with ffmpeg (bundled via imageio-ffmpeg)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

ProgressFn = Callable[[float, str], None]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def to_int16(audio: np.ndarray) -> bytes:
    a = np.clip(audio, -1.0, 1.0)
    return (a * 32767.0).astype("<i2").tobytes()


def write_wav(path, audio: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(to_int16(audio))


def silence(seconds: float, sr: int) -> np.ndarray:
    return np.zeros(int(seconds * sr), dtype=np.float32)


def _atempo_chain(speed: float) -> str:
    # atempo accepts 0.5–2.0 per stage.
    stages = []
    s = speed
    while s > 2.0:
        stages.append(2.0)
        s /= 2.0
    while s < 0.5:
        stages.append(0.5)
        s /= 0.5
    stages.append(s)
    return ",".join(f"atempo={x:.4f}" for x in stages)


class ChapterWriter:
    """Streams PCM from the TTS engine into a FLAC file (lossless, ~half the
    size of WAV) that is later encoded to the final format."""

    def __init__(self, path: Path, sr: int, tempo: float = 1.0):
        self.path = Path(path)
        self.partial = self.path.with_suffix(".partial.flac")
        self.sr = sr
        self.tempo = tempo
        self.samples = 0
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0"]
        if abs(tempo - 1.0) > 1e-3:
            cmd += ["-af", _atempo_chain(tempo)]
        cmd += ["-c:a", "flac", "-f", "flac", str(self.partial)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=_NO_WINDOW)

    def write(self, audio: np.ndarray) -> None:
        if audio.size:
            self.proc.stdin.write(to_int16(audio))
            self.samples += audio.size

    def close(self) -> float:
        """Finish the file; returns its duration in seconds."""
        self.proc.stdin.close()
        err = self.proc.stderr.read().decode("utf-8", "replace")
        if self.proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed while writing audio: {err.strip()}")
        os.replace(self.partial, self.path)
        return self.samples / self.sr / self.tempo

    def abort(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        try:
            self.partial.unlink(missing_ok=True)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Final export
# --------------------------------------------------------------------------

@dataclass
class RenderedChapter:
    title: str
    flac: Path
    duration: float


@dataclass
class BookMeta:
    title: str
    author: str
    cover_png: Optional[bytes] = None


def safe_filename(s: str, maxlen: int = 80) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip().rstrip(".")
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen].rstrip() or "Untitled")


def _escape_meta(s: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


def _run(cmd: list[str], total_seconds: float, progress: Optional[ProgressFn], label: str,
         cancel: Optional[Callable[[], bool]] = None) -> None:
    cmd = cmd[:1] + ["-hide_banner", "-loglevel", "error", "-nostats", "-nostdin", "-progress", "pipe:1"] + cmd[1:]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    import threading
    err_lines: list[str] = []
    t = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
    t.start()
    for line in proc.stdout:
        if cancel and cancel():
            proc.kill()
            proc.wait()
            raise RuntimeError("Cancelled")
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                us = int(line.split("=", 1)[1])
                if progress and total_seconds > 0:
                    progress(min(1.0, us / 1e6 / total_seconds), label)
            except ValueError:
                pass
    rc = proc.wait()
    t.join(timeout=2)
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed ({label}): {''.join(err_lines).strip()[-800:]}")


def _encode_to(final: Path, build_cmd, *run_args) -> None:
    """Encode to a temporary name and move into place only on success, so a
    cancelled export never leaves a truncated file behind."""
    tmp = final.with_name(final.stem + ".partial" + final.suffix)
    try:
        _run(build_cmd(tmp), *run_args)
        os.replace(tmp, final)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _cover_args(cover_file: Optional[Path], input_index: int) -> tuple[list[str], list[str]]:
    if not cover_file:
        return [], []
    return (["-i", str(cover_file)],
            ["-map", f"{input_index}:v", "-c:v", "mjpeg", "-vf", "scale='min(800,iw)':-2",
             "-disposition:v:0", "attached_pic", "-metadata:s:v", "title=Cover",
             "-metadata:s:v", "comment=Cover (front)"])


_LOUDNORM = "loudnorm=I=-18:TP=-1.5:LRA=11"


def export(
    chapters: list[RenderedChapter],
    meta: BookMeta,
    out_dir: Path,
    mode: str,  # "mp3_chapters" | "mp3_single" | "m4b"
    bitrate_kbps: int = 64,
    normalize: bool = True,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ff = ffmpeg_exe()
    total = sum(c.duration for c in chapters) or 1.0
    tmp = Path(tempfile.mkdtemp(prefix="audiobooker_"))
    cover_file = None
    if meta.cover_png:
        cover_file = tmp / "cover.png"
        cover_file.write_bytes(meta.cover_png)
    af = ["-af", _LOUDNORM, "-ar", "44100"] if normalize else ["-ar", "44100"]
    tags = ["-metadata", f"album={meta.title}", "-metadata", f"artist={meta.author}",
            "-metadata", f"album_artist={meta.author}", "-metadata", "genre=Audiobook"]
    outputs: list[Path] = []

    try:
        if mode == "mp3_chapters":
            folder = out_dir / safe_filename(meta.title)
            folder.mkdir(parents=True, exist_ok=True)
            done = 0.0
            width = max(2, len(str(len(chapters))))
            for i, ch in enumerate(chapters, 1):
                name = f"{str(i).zfill(width)} - {safe_filename(ch.title, 60)}.mp3"
                out = folder / name
                cin, cmap = _cover_args(cover_file, 1)
                def cmd(target, ch=ch, cin=cin, cmap=cmap, i=i):
                    return [ff, "-y", "-i", str(ch.flac), *cin, "-map", "0:a", *cmap, *af,
                            "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k",
                            "-id3v2_version", "3", *tags,
                            "-metadata", f"title={ch.title}", "-metadata", f"track={i}/{len(chapters)}",
                            "-f", "mp3", str(target)]

                def sub(f, m, done=done, d=ch.duration, i=i):
                    if progress:
                        progress((done + f * d) / total, f"Encoding chapter {i} of {len(chapters)}…")
                _encode_to(out, cmd, ch.duration, sub, f"chapter {i}", cancel)
                done += ch.duration
                outputs.append(out)
            # A playlist so players keep the order.
            (folder / f"{safe_filename(meta.title)}.m3u").write_text(
                "#EXTM3U\n" + "".join(
                    f"#EXTINF:{int(c.duration)},{c.title}\n{p.name}\n" for c, p in zip(chapters, outputs)
                ), encoding="utf-8")
        else:
            listfile = tmp / "list.txt"
            listfile.write_text("ffconcat version 1.0\n" + "".join(
                "file '" + c.flac.as_posix().replace("'", "'\\''") + "'\n" for c in chapters),
                encoding="utf-8")
            metafile = tmp / "chapters.txt"
            lines = [";FFMETADATA1",
                     f"title={_escape_meta(meta.title)}",
                     f"artist={_escape_meta(meta.author)}",
                     f"album={_escape_meta(meta.title)}",
                     f"album_artist={_escape_meta(meta.author)}",
                     "genre=Audiobook"]
            t = 0.0
            for ch in chapters:
                start = int(round(t * 1000))
                t += ch.duration
                lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}",
                          f"END={int(round(t * 1000))}", f"title={_escape_meta(ch.title)}"]
            metafile.write_text("\n".join(lines) + "\n", encoding="utf-8")

            ext = "mp3" if mode == "mp3_single" else "m4b"
            out = out_dir / f"{safe_filename(meta.title)}.{ext}"
            cin, cmap = _cover_args(cover_file, 2)
            def cmd(target):
                c = [ff, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-i", str(metafile), *cin,
                     "-map", "0:a", *cmap, "-map_metadata", "1", "-map_chapters", "1", *af]
                if ext == "mp3":
                    c += ["-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k", "-id3v2_version", "3", "-f", "mp3"]
                else:
                    c += ["-c:a", "aac", "-b:a", f"{bitrate_kbps}k", "-movflags", "+faststart", "-f", "mp4"]
                return c + [str(target)]
            _encode_to(out, cmd, total, lambda f, m: progress and progress(f, f"Encoding {ext.upper()}…"),
                 ext, cancel)
            outputs.append(out)
    finally:
        for p in tmp.iterdir():
            try:
                p.unlink()
            except Exception:
                pass
        try:
            tmp.rmdir()
        except Exception:
            pass
    return outputs
