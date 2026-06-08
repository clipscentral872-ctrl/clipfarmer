"""Generate a clean placeholder demographics PNG for first-submission use.

For brand-new accounts the system doesn't have real analytics yet, but
Whop's submission form requires *something* in the demographics image
field. This script renders a presentable 1080x1920 image with a clear
'analytics will follow' message so the form passes and the reviewer
knows the real data is incoming via the 48hr support-chat workflow.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont


def main() -> int:
    out_path = Path("data/screenshots/placeholder-demographics.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    W, H = 1080, 1920
    bg = (20, 24, 40)
    accent = (255, 120, 40)
    text_main = (255, 255, 255)
    text_sub = (180, 185, 200)

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    font_paths = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]

    def _font(size: int):
        for p in font_paths:
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
        return ImageFont.load_default()

    f_title = _font(80)
    f_body = _font(50)
    f_small = _font(40)

    # Header accent bar
    d.rectangle((0, 0, W, 12), fill=accent)

    title_lines = ["Analytics", "Pending"]
    y = 380
    for ln in title_lines:
        bbox = d.textbbox((0, 0), ln, font=f_title)
        d.text(((W - (bbox[2] - bbox[0])) / 2, y), ln, font=f_title, fill=text_main)
        y += 110

    y += 60
    body_lines = [
        "Views, country mix and age",
        "demographics will be shared via",
        "the campaign's Support Chat",
        "48 hours after this post goes live,",
        "per the Whop submission process.",
    ]
    for ln in body_lines:
        bbox = d.textbbox((0, 0), ln, font=f_body)
        d.text(((W - (bbox[2] - bbox[0])) / 2, y), ln, font=f_body, fill=text_sub)
        y += 75

    foot = "First submission - analytics to follow"
    bbox = d.textbbox((0, 0), foot, font=f_small)
    d.text(((W - (bbox[2] - bbox[0])) / 2, H - 180), foot, font=f_small, fill=accent)

    # Footer accent bar
    d.rectangle((0, H - 12, W, H), fill=accent)

    img.save(out_path, "PNG", optimize=True)
    abs_path = out_path.resolve()
    print(f"Saved: {abs_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
