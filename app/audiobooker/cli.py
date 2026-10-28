"""Command-line interface: python -m audiobooker.cli book.pdf [options]

Handy for batch jobs and for testing without the GUI.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from . import paths

paths.configure_model_cache()

from . import job, pdf_extract  # noqa: E402
from .engines import engine_classes, get_engine  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="audiobooker", description="Turn a PDF into an audiobook.")
    ap.add_argument("pdf")
    ap.add_argument("--engine", default="kokoro", choices=[c.key for c in engine_classes()])
    ap.add_argument("--voice", default=None)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--format", dest="mode", default="m4b", choices=["m4b", "mp3_single", "mp3_chapters"])
    ap.add_argument("--bitrate", type=int, default=64)
    ap.add_argument("--out", default="")
    ap.add_argument("--ocr", default="auto", choices=["auto", "always", "never"])
    ap.add_argument("--chapters", default="", help="e.g. 1-3,5 (default: auto-selected chapters)")
    ap.add_argument("--list", action="store_true", help="only list detected chapters")
    ap.add_argument("--list-voices", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    eng = get_engine(args.engine)
    if args.list_voices:
        for v in eng.voices():
            print(f"{v.id:24} {v.label}")
        return 0

    def p(f, m):
        print(f"\r[{f * 100:5.1f}%] {m[:100]:100}", end="", flush=True)

    book = pdf_extract.load_book(args.pdf, ocr=args.ocr, progress=p)
    print()
    print(f"{book.title!r} by {book.author or 'unknown'} — {book.page_count} pages, "
          f"chapters from {book.chapter_source}, {book.ocr_pages} OCR pages")
    for w in book.warnings:
        print("warning:", w)
    if args.chapters:
        wanted = set()
        for part in args.chapters.split(","):
            a, _, b = part.partition("-")
            wanted.update(range(int(a), int(b or a) + 1))
        for i, c in enumerate(book.chapters, 1):
            c.include = i in wanted
    for i, c in enumerate(book.chapters, 1):
        print(f"{'x' if c.include else ' '} {i:3}. {c.title[:60]:60} p{c.start_page}-{c.end_page} {c.word_count:7} words")
    if args.list:
        return 0

    s = job.JobSettings(engine=args.engine, voice=args.voice or eng.voices()[0].id, speed=args.speed,
                        output_mode=args.mode, bitrate_kbps=args.bitrate, out_dir=args.out)
    t0 = time.time()
    outs = job.run_job(book, s, eng, lambda pr: p(pr.fraction, pr.message), lambda: False)
    print(f"\nDone in {time.time() - t0:.0f}s:")
    for o in outs:
        print("  ", o)
    return 0


if __name__ == "__main__":
    sys.exit(main())
