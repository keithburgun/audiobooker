"""PDF -> clean, chapterised text.

The pipeline:
  1. Pull every text line off every page with its position and font size
     (or OCR the page if it is a scan with no text layer).
  2. Drop running headers/footers, page numbers, footnotes and footnote
     markers.
  3. Rebuild paragraphs (undoing line wraps, hyphenation and paragraphs
     split across page breaks).
  4. Split into chapters using the PDF outline (bookmarks) if it has one,
     otherwise by recognising chapter headings.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:
    import pymupdf as fitz  # PyMuPDF
except ImportError:  # older PyMuPDF
    import fitz

from . import paths

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Paragraph:
    text: str
    kind: str = "body"  # "body" | "heading"


@dataclass
class Chapter:
    title: str
    paragraphs: list[Paragraph]
    start_page: int  # 1-based
    end_page: int
    include: bool = True

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.paragraphs)

    @property
    def word_count(self) -> int:
        return sum(len(p.text.split()) for p in self.paragraphs)


@dataclass
class Book:
    path: str
    title: str
    author: str
    page_count: int
    chapters: list[Chapter]
    chapter_source: str  # "outline" | "headings" | "none"
    ocr_pages: int = 0
    cover_png: Optional[bytes] = None
    warnings: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(c.word_count for c in self.chapters if c.include)


@dataclass
class _Line:
    page: int  # 0-based
    y0: float
    y1: float
    x0: float
    text: str
    size: float
    bold: bool
    block: int
    page_h: float
    ocr: bool = False


@dataclass
class _Para:
    page: int
    y0: float
    text: str
    size: float
    bold: bool
    lines: int
    last_page: int = -1


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_PAGE_NUM_RE = re.compile(
    r"^(page\s*)?([\divxlcdm]+)(\s*(of|/)\s*\d+)?$", re.IGNORECASE
)
_TERMINAL_RE = re.compile(r"[.!?:;\"'”’»)\]…]$")
_CHAPTER_WORDS = (
    "chapter|part|book|prologue|epilogue|introduction|preface|foreword|"
    "afterword|interlude|appendix|conclusion|acknowledgments|acknowledgements"
)
_HEADING_RE = re.compile(
    rf"^((({_CHAPTER_WORDS})\b.*)|([ivxlc]+\.?)|(\d{{1,3}}\.?))$", re.IGNORECASE
)
_SKIP_TITLES_RE = re.compile(
    r"^(cover|title page|half title|copyright|contents|table of contents|index|"
    r"bibliography|references|works cited|endnotes|notes|also by.*|praise for.*|"
    r"other books by.*|about the publisher|front matter)$",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def _clean_inline(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("­", "")  # soft hyphen
    s = s.replace("�", "")
    s = re.sub(r"[ \t ]+", " ", s)
    return s.strip()


def _is_page_number(text: str) -> bool:
    t = text.strip().strip("-–—[]() ")
    return bool(t) and len(t) <= 14 and bool(_PAGE_NUM_RE.match(t))


def _check(cancel: Optional[CancelFn]) -> None:
    if cancel and cancel():
        raise Cancelled()


# --------------------------------------------------------------------------
# Line extraction (native text layer)
# --------------------------------------------------------------------------

def _native_lines(page: fitz.Page, pno: int) -> list[_Line]:
    out: list[_Line] = []
    # Content-stream order (not sort=True) keeps two-column pages in reading order.
    d = page.get_text("dict")
    ph = page.rect.height
    for bno, block in enumerate(d.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            if line.get("dir", (1, 0))[0] < 0.5:  # skip rotated/vertical text
                continue
            spans = line.get("spans", [])
            if not spans:
                continue
            # Body font size of this line = size of the longest span.
            main = max(spans, key=lambda s: len(s.get("text", "").strip()) or 0)
            main_size = main.get("size", 10.0)
            parts = []
            for s in spans:
                t = s.get("text", "")
                flags = s.get("flags", 0)
                # Footnote markers: superscript (flag bit 0) digits/symbols.
                if (flags & 1) and re.fullmatch(r"\s*[\d*†‡§¶]+\s*", t):
                    continue
                if s.get("size", main_size) < main_size * 0.72 and re.fullmatch(r"\s*\d{1,3}\s*", t):
                    continue  # tiny raised number that isn't flagged as superscript
                parts.append(t)
            text = _clean_inline("".join(parts))
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            bold = bool(main.get("flags", 0) & 16) or "bold" in main.get("font", "").lower()
            out.append(_Line(pno, y0, y1, x0, text, round(main_size * 2) / 2, bold, bno, ph))
    return out


# --------------------------------------------------------------------------
# OCR (scanned pages)
# --------------------------------------------------------------------------

_ocr_engine = None


def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR  # bundled models, CPU, no install
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_lines(page: fitz.Page, pno: int, dpi: int = 200) -> list[dict]:
    import numpy as np

    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1]
    result, _ = _get_ocr()(np.ascontiguousarray(img))
    lines = []
    for box, text, score in result or []:
        if float(score) < 0.5 or not text.strip():
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append({
            "x0": min(xs) / scale, "x1": max(xs) / scale, "y0": min(ys) / scale,
            "y1": max(ys) / scale, "text": text,
        })
    return lines


def _ocr_to_lines(raw: list[dict], pno: int, page_h: float) -> list[_Line]:
    if not raw:
        return []
    raw = sorted(raw, key=lambda r: (round(r["y0"] / 4), r["x0"]))
    med_h0 = statistics.median([r["y1"] - r["y0"] for r in raw])
    # Two-column scans: if many rows have a wide horizontal gap at about the
    # same x position, read the left column top-to-bottom, then the right.
    width = max(r.get("x1", r["x0"]) for r in raw)
    gaps = []
    for a, b in zip(raw, raw[1:]):
        same_row = abs(a["y0"] - b["y0"]) < 0.5 * (a["y1"] - a["y0"])
        if same_row and a.get("x1") and b["x0"] - a["x1"] > 2 * med_h0:
            gaps.append((a["x1"] + b["x0"]) / 2)
    if len(gaps) >= max(4, len(raw) // 5):
        split = statistics.median(gaps)
        if 0.3 * width < split < 0.7 * width:
            raw = sorted(raw, key=lambda r: (0 if r["x0"] < split else 1, r["y0"], r["x0"]))
    # Merge fragments on the same visual row (but not across a column gap).
    rows: list[dict] = []
    for r in raw:
        if rows and abs(r["y0"] - rows[-1]["y0"]) < 0.5 * (rows[-1]["y1"] - rows[-1]["y0"]) \
                and r["x0"] - rows[-1].get("x1", r["x0"]) <= 2 * med_h0:
            rows[-1]["text"] += " " + r["text"]
            rows[-1]["y1"] = max(rows[-1]["y1"], r["y1"])
            rows[-1]["x1"] = max(rows[-1].get("x1", 0), r.get("x1", 0))
        else:
            rows.append(dict(r))
    heights = [r["y1"] - r["y0"] for r in rows]
    med_h = statistics.median(heights) if heights else 12
    out: list[_Line] = []
    block = 0
    prev_y1 = None
    prev_x0 = None
    for r in rows:
        h = r["y1"] - r["y0"]
        if prev_y1 is not None:
            gap = r["y0"] - prev_y1
            indented = prev_x0 is not None and r["x0"] - prev_x0 > med_h * 0.8
            if gap > med_h * 0.9 or indented:
                block += 1
        text = _clean_inline(r["text"])
        # OCR boxes don't give a font size. Average glyph width is a steadier
        # estimate than box height (~0.47 em per character for book faces).
        if r.get("x1") and len(text) >= 3:
            size = (r["x1"] - r["x0"]) / len(text) / 0.47
        else:
            size = h * 0.75
        size = round(size * 2) / 2
        text = re.sub(r"\b(CHAPTER|PART|BOOK)(?=[A-Z0-9])", r"\1 ", text)
        out.append(_Line(pno, r["y0"], r["y1"], r["x0"], text, size, False, block, page_h, ocr=True))
        prev_y1 = r["y1"]
        prev_x0 = r["x0"]
    return out


def _file_hash(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------

def _strip_margins(lines: list[_Line], n_pages: int, known_titles: set[str] = frozenset()) -> list[_Line]:
    """Remove running headers/footers and page numbers."""
    body = _body_size(lines)
    by_page: dict[int, list[_Line]] = {}
    for ln in lines:
        by_page.setdefault(ln.page, []).append(ln)

    def margin_candidates(pl: list[_Line]) -> list[_Line]:
        pl = sorted(pl, key=lambda l: l.y0)
        cands = []
        for ln in pl[:2] + pl[-2:]:
            frac = ln.y0 / ln.page_h if ln.page_h else 0.5
            if (frac < 0.12 or frac > 0.86) and len(ln.text) < 90:
                cands.append(ln)
        return cands

    counts: Counter[str] = Counter()
    for pl in by_page.values():
        seen = set()
        for ln in margin_candidates(pl):
            key = re.sub(r"\d+", "#", _norm(ln.text)).replace(" ", "")
            if key and key not in seen:
                counts[key] += 1
                seen.add(key)

    min_repeat = 3 if n_pages >= 6 else 2
    drop: set[int] = set()
    for pl in by_page.values():
        for ln in margin_candidates(pl):
            key = re.sub(r"\d+", "#", _norm(ln.text)).replace(" ", "")
            small = ln.size <= body * 0.9
            top = ln.page_h and ln.y0 / ln.page_h < 0.1
            if ln.size >= body * 1.1 or (ln.bold and not small):
                continue  # a real heading, not a running head
            looks_like_heading = _HEADING_RE.match(ln.text.strip()) is not None
            if (
                (_is_page_number(ln.text) and not (top and ln.size >= body * 1.05))
                or (key and counts[key] >= min_repeat and not (looks_like_heading and not small))
                # Small running heads that only repeat a couple of times
                # (chapter-title headers in short chapters).
                or (key and small and counts[key] >= 2)
                # A small line at the very top that is just a chapter title.
                or (top and small and _norm(ln.text) in known_titles)
                # Tiny text in the very top margin is a running head.
                or (ln.page_h and ln.y0 / ln.page_h < 0.075 and ln.size <= body * 0.88 and len(ln.text) < 70)
            ):
                drop.add(id(ln))
    return [ln for ln in lines if id(ln) not in drop]


def _body_size(lines: list[_Line]) -> float:
    c: Counter[float] = Counter()
    for ln in lines:
        c[ln.size] += len(ln.text)
    return c.most_common(1)[0][0] if c else 10.0


def _strip_footnotes(lines: list[_Line], body: float) -> list[_Line]:
    """Drop footnote blocks: small text in the lower part of the page that
    starts with a note marker, or that is separated from the text above by a
    clear gap. Smaller-type block quotes and verse in the flow are kept."""
    limit = lambda ln: body * (0.8 if ln.ocr else 0.85)  # noqa: E731
    by_page: dict[int, list[_Line]] = {}
    for ln in lines:
        by_page.setdefault(ln.page, []).append(ln)
    drop: set[int] = set()
    for pl in by_page.values():
        pl_sorted = sorted(pl, key=lambda l: l.y0)
        in_notes = False
        prev = None
        for ln in pl_sorted:
            frac = ln.y0 / ln.page_h if ln.page_h else 0
            small = ln.size <= limit(ln)
            if in_notes and small:
                drop.add(id(ln))
            elif small and frac > 0.55:
                gap = (ln.y0 - prev.y1) if prev else 0
                marker = re.match(r"^\s*([\d]{1,3}|[*†‡§¶])[\s.)]", ln.text) is not None
                if marker or gap > body * 1.6:
                    in_notes = True
                    drop.add(id(ln))
            else:
                in_notes = False if not small else in_notes
            prev = ln
    return [ln for ln in lines if id(ln) not in drop]


def _join_lines(texts: list[str]) -> str:
    s = ""
    for t in texts:
        if not s:
            s = t
        elif re.search(r"[A-Za-z]-$", s) and t[:1].islower():
            s = s[:-1] + t  # undo end-of-line hyphenation
        elif s.endswith(("—", "–", "/")):
            s += t
        else:
            s += " " + t
    return s


def _build_paragraphs(lines: list[_Line], body: float) -> list[_Para]:
    paras: list[_Para] = []
    cur: list[_Line] = []

    def flush():
        if cur:
            paras.append(_Para(
                page=cur[0].page, y0=cur[0].y0,
                text=_join_lines([l.text for l in cur]),
                size=max(l.size for l in cur), bold=all(l.bold for l in cur),
                lines=len(cur), last_page=cur[-1].page,
            ))
            cur.clear()

    for ln in lines:
        if cur:
            prev = cur[-1]
            new_block = ln.block != prev.block or ln.page != prev.page
            size_change = abs(ln.size - prev.size) > 0.75
            big_gap = (ln.y0 - prev.y1) > max(prev.size, ln.size) * 0.9
            # Indented first line inside a single PDF block = new paragraph
            indent = (not ln.ocr) and (ln.x0 - cur[0].x0) > body * 1.2 and len(cur) > 1 \
                and _TERMINAL_RE.search(prev.text) is not None
            if new_block or size_change or big_gap or indent:
                flush()
        cur.append(ln)
    flush()

    # Merge paragraphs broken across columns/pages: previous one doesn't end a
    # sentence and the next one continues in lower case.
    merged: list[_Para] = []
    for p in paras:
        if merged:
            q = merged[-1]
            continues = p.text[:1].islower() or (
                p.page != q.last_page and abs(q.size - body) <= 0.75 and not q.text.endswith((":", "—"))
                and len(q.text) > 80
            )
            if (
                not _TERMINAL_RE.search(q.text)
                and continues
                and abs(q.size - p.size) <= 0.75
            ):
                q.text = _join_lines([q.text, p.text])
                q.lines += p.lines
                q.last_page = p.last_page
                continue
        merged.append(p)
    return merged


# --------------------------------------------------------------------------
# Chapter detection
# --------------------------------------------------------------------------

def _is_heading_like(p: _Para, body: float) -> bool:
    words = p.text.split()
    if not words or len(words) > 14 or len(p.text) > 100 or p.lines > 3:
        return False
    if p.text.endswith((".", ",", ";")) and not _HEADING_RE.match(p.text):
        return False
    if re.search(r"(\.{3,}|\s{2,}|\t|[.·…]\s?[.·…]\s?[.·…])[\s.·…]*[\divxlc]+\.?$", p.text, re.IGNORECASE):
        return False  # a table-of-contents line ("Chapter One ...... 3")
    return True


def _chapters_from_outline(doc: fitz.Document, paras: list[_Para], body: float) -> Optional[list[tuple[int, str]]]:
    """Return [(para_index, title)] boundaries from the PDF outline."""
    toc = doc.get_toc(simple=True)
    toc = [(lvl, t.strip(), pg) for lvl, t, pg in toc if t and t.strip() and pg >= 1]
    if len(toc) < 2:
        return None
    levels = Counter(lvl for lvl, t, _ in toc if not _SKIP_TITLES_RE.match(t))
    # Use level 1, unless it is just a handful of "Part" entries with many
    # chapters nested underneath: then go one level deeper.
    level = 1
    while (
        level < 3
        and levels.get(level + 1, 0) >= max(2, levels.get(level, 0) * 2)
        and (levels.get(level, 0) < 4 or levels.get(level + 1, 0) >= levels.get(level, 0) * 3)
    ):
        level += 1
    entries = [(t, pg) for lvl, t, pg in toc if lvl <= level]
    entries.sort(key=lambda e: e[1])

    page_first: dict[int, int] = {}
    for i, p in enumerate(paras):
        page_first.setdefault(p.page, i)

    def first_para_on_or_after(page0: int) -> int:
        for pg in range(page0, page0 + 50):
            if pg in page_first:
                return page_first[pg]
        return len(paras)

    bounds: list[tuple[int, str]] = []
    for title, pg in entries:
        start = first_para_on_or_after(pg - 1)
        nt = _norm(title)
        # Look for the heading text itself on that page so mid-page chapter
        # starts are split at the right place.
        i = start
        while i < len(paras) and paras[i].page == pg - 1:
            np_ = _norm(paras[i].text)
            if np_ and len(np_) >= 2 and _is_heading_like(paras[i], body) and (
                np_ in nt or nt in np_ or nt.startswith(np_) or np_.startswith(nt[:20])
            ):
                start = i
                break
            i += 1
        if bounds and start <= bounds[-1][0]:
            # Two outline entries landing on the same spot: keep the later title.
            if start == bounds[-1][0]:
                bounds[-1] = (start, title)
            continue
        bounds.append((start, title))
    return bounds if len(bounds) >= 2 else None


def _keyword_heading(p: _Para, body: float) -> bool:
    t = p.text.strip()
    if not (_is_heading_like(p, body) and _HEADING_RE.match(t)):
        return False
    letters = [c for c in t if c.isalpha()]
    caps = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.8
    prominent = p.size >= body * 1.1 or p.bold or caps
    if re.fullmatch(r"[ivxlc\d]+\.?", t, re.IGNORECASE):
        return p.size >= body * 1.15
    # "Part of me wanted to stay." is dialogue, "Part Two." is a heading.
    if re.search(r"[.!?,;]$", t) and not re.fullmatch(rf"({_CHAPTER_WORDS})(\s+\S+)?\.?", t, re.IGNORECASE):
        return False
    if not prominent and not re.fullmatch(rf"({_CHAPTER_WORDS})\s+([ivxlc\d]+|\w+)([:.\s].*)?", t, re.IGNORECASE):
        return False
    return prominent or p.lines == 1 and len(t.split()) <= 6


def _chapters_from_headings(paras: list[_Para], body: float, n_pages: int) -> Optional[list[tuple[int, str]]]:
    regex_hits = [i for i, p in enumerate(paras) if _keyword_heading(p, body)]
    named = [i for i in regex_hits if re.match(rf"^({_CHAPTER_WORDS})\b", paras[i].text, re.IGNORECASE)]
    hits = named if len(named) >= 2 else regex_hits
    if len(hits) < 2:
        hits = [
            i for i, p in enumerate(paras)
            if _is_heading_like(p, body) and p.size >= body * 1.3
        ]
        if not (2 <= len(hits) <= max(3, n_pages // 2)):
            return None
    bounds: list[tuple[int, str]] = []
    for i in hits:
        title = paras[i].text.strip()
        # "CHAPTER ONE" followed by "The Storm" -> one title.
        j = i + 1
        if j < len(paras) and j not in hits and _is_heading_like(paras[j], body) and \
                paras[j].page == paras[i].page and paras[j].size >= body * 1.1:
            title = f"{_title_case_if_shouting(title)}: {_title_case_if_shouting(paras[j].text.strip())}"
        if bounds and i - bounds[-1][0] <= 1:
            continue
        bounds.append((i, title))
    return bounds


def _title_case_if_shouting(s: str) -> str:
    letters = [c for c in s if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        words = []
        for w in s.split():
            if re.fullmatch(r"[IVXLC]+\.?", w):
                words.append(w)
            else:
                words.append(w.capitalize())
        return " ".join(words)
    return s


def _make_chapters(paras: list[_Para], bounds: list[tuple[int, str]], body: float) -> list[Chapter]:
    chapters: list[Chapter] = []
    if bounds[0][0] > 0:
        front = paras[: bounds[0][0]]
        if sum(len(p.text.split()) for p in front) > 0:
            chapters.append(_chapter_from("Front matter", front, body, include=False))
    for k, (start, title) in enumerate(bounds):
        end = bounds[k + 1][0] if k + 1 < len(bounds) else len(paras)
        chunk = paras[start:end]
        if not chunk:
            continue
        chapters.append(_chapter_from(title, chunk, body))

    # Fold near-empty chapters (e.g. a "Part One" divider page) into the next.
    folded: list[Chapter] = []
    carry: Optional[Chapter] = None
    for ch in chapters:
        if carry is not None:
            ch.paragraphs = carry.paragraphs + ch.paragraphs
            ch.start_page = carry.start_page
            carry = None
        if ch.word_count < 25 and ch is not chapters[-1] and ch.include:
            carry = ch
            continue
        folded.append(ch)
    if carry is not None:
        folded.append(carry)
    return folded


def _chapter_from(title: str, chunk: list[_Para], body: float, include: bool = True) -> Chapter:
    title = _title_case_if_shouting(re.sub(r"\s+", " ", title).strip())
    out: list[Paragraph] = []
    for idx, p in enumerate(chunk):
        heading = idx < 3 and _is_heading_like(p, body) and (
            p.size >= body * 1.1 or p.bold or _HEADING_RE.match(p.text.strip()) is not None
            or _norm(p.text) in _norm(title)
        ) and all(q.kind == "heading" for q in out)
        text = _title_case_if_shouting(p.text) if heading else p.text
        out.append(Paragraph(text, "heading" if heading else "body"))
    # If the chapter title isn't printed in the text (or is an image), say it.
    if not any(q.kind == "heading" for q in out) and title != "Front matter":
        out.insert(0, Paragraph(title, "heading"))
    if _SKIP_TITLES_RE.match(title.strip()):
        include = False
    return Chapter(title=title, paragraphs=out, start_page=chunk[0].page + 1, end_page=max(chunk[-1].page, chunk[-1].last_page) + 1, include=include)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def load_book(
    path: str,
    ocr: str = "auto",  # "auto" | "always" | "never"
    skip_footnotes: bool = True,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> Book:
    progress = progress or (lambda f, m: None)
    doc = fitz.open(path)
    n = doc.page_count
    warnings: list[str] = []

    ocr_cache_file = paths.jobs_dir() / f"ocr2_{_file_hash(path)}.json"
    try:
        ocr_cache: dict[str, list] = json.loads(ocr_cache_file.read_text("utf-8")) if ocr_cache_file.exists() else {}
    except Exception:
        ocr_cache = {}
    ocr_dirty = False

    lines: list[_Line] = []
    ocr_pages = 0
    for pno in range(n):
        _check(cancel)
        page = doc[pno]
        native = _native_lines(page, pno)
        chars = sum(len(l.text) for l in native)
        needs_ocr = ocr == "always" or (
            ocr == "auto" and chars < 25 and (page.get_images(full=False) or page.get_drawings())
        )
        if needs_ocr:
            progress(pno / n, f"Reading scanned page {pno + 1} of {n} (OCR)…")
            key = str(pno)
            if key not in ocr_cache:
                try:
                    ocr_cache[key] = _ocr_lines(page, pno)
                    ocr_dirty = True
                except Exception as e:  # keep going; report once
                    if not any("OCR" in w for w in warnings):
                        warnings.append(f"OCR failed on some pages: {e}")
                    ocr_cache[key] = []
            got = _ocr_to_lines(ocr_cache[key], pno, page.rect.height)
            if got:
                ocr_pages += 1
                lines.extend(got)
            else:
                lines.extend(native)
            if ocr_dirty and pno % 10 == 0:
                ocr_cache_file.write_text(json.dumps(ocr_cache), "utf-8")
        else:
            if pno % 20 == 0:
                progress(pno / n, f"Reading page {pno + 1} of {n}…")
            lines.extend(native)
    if ocr_dirty:
        ocr_cache_file.write_text(json.dumps(ocr_cache), "utf-8")

    progress(0.97, "Finding chapters…")
    titles = {_norm(t) for _, t, _ in doc.get_toc(simple=True) if t}
    lines = _strip_margins(lines, n, titles)
    body = _body_size(lines)
    if skip_footnotes:
        lines = _strip_footnotes(lines, body)
    paras = _build_paragraphs(lines, body)
    paras = [p for p in paras if re.search(r"\w", p.text)]

    if not paras:
        raise ValueError("No readable text was found in this PDF.")

    bounds = _chapters_from_outline(doc, paras, body)
    source = "outline"
    if not bounds:
        bounds = _chapters_from_headings(paras, body, n)
        source = "headings"
    if not bounds:
        bounds = [(0, "Full text")]
        source = "none"
        warnings.append("No chapters were detected, so the book is treated as one long chapter.")
    chapters = _make_chapters(paras, bounds, body)

    meta = doc.metadata or {}
    title = (meta.get("title") or "").strip() or Path(path).stem.replace("_", " ")
    author = (meta.get("author") or "").strip()

    cover = None
    try:
        pg = doc[0]
        zoom = 800 / max(pg.rect.width, 1)
        cover = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")
    except Exception:
        pass

    doc.close()
    progress(1.0, "Done")
    return Book(
        path=str(path), title=title, author=author, page_count=n, chapters=chapters,
        chapter_source=source, ocr_pages=ocr_pages, cover_png=cover, warnings=warnings,
    )
