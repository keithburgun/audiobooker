"""Draws the Audiobooker icon (open book + sound waves) as PNG and ICO."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# background tile
d.rounded_rectangle((40, 40, S - 40, S - 40), radius=210, fill=(36, 41, 86, 255))
d.rounded_rectangle((40, 40, S - 40, S // 2), radius=210, fill=(48, 55, 112, 255))
d.rectangle((40, S // 2 - 210, S - 40, S // 2), fill=(48, 55, 112, 255))

# open book
cream = (250, 240, 220, 255)
shade = (226, 211, 184, 255)
cx, top, bottom = S // 2, 330, 760
d.polygon([(cx, top + 40), (190, top), (190, bottom - 40), (cx, bottom)], fill=cream)
d.polygon([(cx, top + 40), (S - 190, top), (S - 190, bottom - 40), (cx, bottom)], fill=shade)
for i in range(5):  # text lines on the left page
    y = top + 90 + i * 62
    d.line([(240, y - 8), (cx - 50, y + 22)], fill=(170, 160, 140, 255), width=16)

# sound waves on the right page
wc = (245, 166, 35, 255)
for i, r in enumerate((90, 160, 230)):
    bbox = (cx + 40 - r, 545 - r, cx + 40 + r, 545 + r)
    d.arc(bbox, start=-50, end=50, fill=wc, width=34)
d.ellipse((cx + 10, 515, cx + 70, 575), fill=wc)

img = img.resize((512, 512), Image.LANCZOS)
img.save(out / "icon.png")
img.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written to", out)
