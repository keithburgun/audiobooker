"""Generate small synthetic books that exercise the extractor:
running headers, page numbers, footnotes, hyphenation, outline vs. headings,
mid-page chapter starts and scanned (image-only) pages."""
import sys
import textwrap
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
OUT.mkdir(parents=True, exist_ok=True)

PARAS = [
    "The lighthouse stood at the edge of the world, or so it seemed to Mara on the morning she first climbed its "
    "spiral stairs. The keeper, an old man named Tobias, had warned her about the wind, but nothing could have "
    "prepared her for the way it howled through the iron railings like something alive.",
    "She spent the first week learning the rhythm of the place: the lamp at dusk, the logbook at midnight, the "
    "long careful walk along the rocks at dawn. Tobias rarely spoke, and when he did it was usually about the "
    "weather, which he described with the reverence other men reserve for scripture.",
    "On the eighth day a ship appeared on the horizon. It did not move. Mara watched it through the brass telescope "
    "for an hour, then two, and still it sat there, a grey smudge against a grey sea, as patient as a heron.",
    "“What do you make of it?” she asked at supper. Tobias chewed slowly, swallowed, and set down his fork. "
    "“I make nothing of it,” he said. “And neither should you.”",
]
CHAPTERS = [("Chapter One", "The Keeper"), ("Chapter Two", "The Ship"), ("Chapter Three", "The Storm")]


def layout(doc, chapters, outline=True, header=True, footnote=True, title="The Lighthouse"):
    W, H = 432, 648  # 6x9 in
    margin, lh, fs = 54, 15, 11
    toc = []
    page = None
    y = 0
    pno = 0

    def new_page(chapter_title):
        nonlocal page, y, pno
        page = doc.new_page(width=W, height=H)
        pno += 1
        if header:
            hdr = title.upper() if pno % 2 == 0 else chapter_title
            page.insert_text((W / 2 - len(hdr) * 2.6, 34), hdr, fontsize=8, fontname="tiro")
        page.insert_text((W / 2 - 4, H - 28), str(pno), fontsize=9, fontname="tiro")
        y = margin + 10

    # Title + contents page
    new_page("")
    page.insert_text((margin, 200), title, fontsize=26, fontname="tibo")
    page.insert_text((margin, 240), "A novella", fontsize=13, fontname="tiro")
    new_page("")
    page.insert_text((margin, y + 20), "Contents", fontsize=18, fontname="tibo")
    for i, (a, b) in enumerate(chapters):
        page.insert_text((margin, y + 60 + i * 20), f"{a}: {b} .......... {3 + i * 2}", fontsize=11, fontname="tiro")
    if outline:
        toc.append([1, "Contents", pno])

    for ci, (label, name) in enumerate(chapters):
        mid_page = ci == 2  # chapter 3 starts mid-page
        if not mid_page:
            new_page(f"{label}: {name}")
            y += 60
        else:
            y += 30
        page.insert_text((margin, y), label.upper(), fontsize=16, fontname="tibo")
        y += 24
        page.insert_text((margin, y), name, fontsize=14, fontname="tiro")
        y += 30
        if outline:
            toc.append([1, f"{label}: {name}", pno])
        paras = PARAS if ci != 1 else PARAS[::-1]
        paras = paras + paras[:2] if ci == 0 else paras
        for pi, para in enumerate(paras):
            lines = textwrap.wrap(para, 62)
            # hyphenate the end of line 1 of every paragraph to test de-hyphenation
            if len(lines) > 2:
                w = lines[1].split()
                last = lines[1].rsplit(" ", 1)
                nxt = lines[2].split(" ", 1)
                if len(nxt[0]) > 6 and nxt[0].isalpha():
                    k = len(nxt[0]) // 2
                    lines[1] = lines[1] + " " + nxt[0][:k] + "-"
                    lines[2] = nxt[0][k:] + (" " + nxt[1] if len(nxt) > 1 else "")
            for li, line in enumerate(lines):
                if y > H - 80:
                    new_page(f"{label}: {name}")
                x = margin + (14 if li == 0 else 0)
                page.insert_text((x, y), line, fontsize=fs, fontname="tiro")
                if footnote and ci == 0 and pi == 1 and li == len(lines) - 1:
                    page.insert_text((x + fitz.get_text_length(line, "tiro", fs) + 1, y - 4), "1",
                                     fontsize=7, fontname="tiro")
                    page.insert_text((margin, H - 52), "1 Tobias kept the logbook in a cipher of his own.",
                                     fontsize=8, fontname="tiro")
                y += lh
            y += 6
        if ci == 1:
            y += 20  # leave room so chapter 3 starts on the same page
    if outline:
        doc.set_toc(toc)
    doc.set_metadata({"title": title, "author": "A. N. Author"})


d = fitz.open()
layout(d, CHAPTERS, outline=True)
d.save(OUT / "outline_book.pdf")

d = fitz.open()
layout(d, CHAPTERS, outline=False, title="Headings Only")
d.set_metadata({})
d.save(OUT / "headings_book.pdf")

# Scanned version: rasterise every page, no text layer.
src = fitz.open(OUT / "headings_book.pdf")
scan = fitz.open()
for p in src:
    pix = p.get_pixmap(dpi=200, colorspace=fitz.csGRAY)
    np_ = scan.new_page(width=p.rect.width, height=p.rect.height)
    np_.insert_image(np_.rect, pixmap=pix)
scan.save(OUT / "scanned_book.pdf")
print("written to", OUT)
