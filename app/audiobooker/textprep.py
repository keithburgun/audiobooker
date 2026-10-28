"""Text normalisation and chunking for TTS engines."""
from __future__ import annotations

import re

_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "st", "sr", "jr", "vs", "etc", "e.g", "i.e",
    "no", "vol", "ch", "fig", "mt", "ft", "gen", "col", "capt", "lt", "rev", "sgt",
    "approx", "dept", "est", "inc", "ltd", "co", "jan", "feb", "mar", "apr", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec", "p", "pp", "cf", "al",
}

_SENT_END = re.compile(r"([.!?…]+[\"'”’)\]]*)(\s+)(?=[\"'“‘(\[]?[A-Z0-9])")


def normalize(text: str) -> str:
    """Light clean-up that helps every engine read naturally."""
    t = text
    t = re.sub(r"https?://\S+|www\.\S+", "", t)  # don't read URLs aloud
    t = re.sub(r"\[\d+(?:[,–-]\d+)*\]", "", t)   # [12] reference markers
    t = t.replace("&", " and ")
    t = t.replace("…", "...")
    t = re.sub(r"\s*—\s*", " — ", t)
    t = re.sub(r"(?<=\w)\s*–\s*(?=\w)", " – ", t)
    t = re.sub(r"[_*#~^|<>]{2,}", " ", t)        # ASCII art / rules
    t = re.sub(r"\.{4,}", "...", t)              # dot leaders
    # Long ALL-CAPS words are shouted headings, not acronyms.
    t = re.sub(r"\b([A-Z]{5,})\b", lambda m: m.group(1).capitalize(), t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end(1)
        before = text[start:end]
        last_word = re.search(r"(\b[\w.]+)\.[\"'”’)\]]*$", before)
        if last_word and last_word.group(1).lower().rstrip(".") in _ABBREV:
            continue
        if re.search(r"\b[A-Z]\.$", before):  # initials: "J. R. R. Tolkien"
            continue
        out.append(before.strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


def _split_long(sentence: str, max_chars: int) -> list[str]:
    if len(sentence) <= max_chars:
        return [sentence]
    # Prefer clause boundaries, then fall back to word boundaries.
    for pat in (r"(?<=[;:])\s+", r"(?<=,)\s+", r"\s+(?=— )", r"\s+"):
        pieces = re.split(pat, sentence)
        if len(pieces) == 1:
            continue
        chunks, cur = [], ""
        for p in pieces:
            if cur and len(cur) + 1 + len(p) > max_chars:
                chunks.append(cur)
                cur = p
            else:
                cur = f"{cur} {p}" if cur else p
        if cur:
            chunks.append(cur)
        if all(len(c) <= max_chars for c in chunks):
            return chunks
        out = []
        for c in chunks:
            out.extend(_split_long(c, max_chars) if len(c) > max_chars else [c])
        return out
    return [sentence[i:i + max_chars] for i in range(0, len(sentence), max_chars)]


def chunk(text: str, max_chars: int) -> list[str]:
    """Group whole sentences into chunks no longer than max_chars."""
    chunks: list[str] = []
    cur = ""
    for s in split_sentences(text):
        for piece in _split_long(s, max_chars):
            if cur and len(cur) + 1 + len(piece) > max_chars:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur} {piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks
