"""Render a clean analytics dashboard PNG from a YouTubeAnalyticsSnapshot.

This is what we send to Whop's campaign Support Chat at the 48hr mark in
place of a hand-screenshotted YT Studio page. It's NOT a fake of YT's
exact UI — it's a clearly-labelled summary of the real numbers, branded
as a clipfarmer analytics card. Reviewers see the same data they'd see
on YT Studio plus the campaign + post URL the views relate to.

Layout (1080x1920 portrait, so it works for both Reels-style upload and
direct Whop attachment):
  - Header bar (accent colour)
  - Big "Total views" number + post title
  - Stats row: likes / comments / watch time / duration
  - Top countries (bar chart)
  - Age + gender split
  - Footer: post URL, captured at, campaign name
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from engine.youtube_analytics import YouTubeAnalyticsSnapshot


# ----------------------------------------------------------------------
def render_analytics_png(
    snap: YouTubeAnalyticsSnapshot,
    *,
    out_path: Path,
    campaign_title: Optional[str] = None,
    post_url: Optional[str] = None,
    captured_at: Optional[str] = None,
) -> Path:
    W, H = 1080, 1920
    bg = (15, 18, 38)
    panel = (24, 27, 53)
    accent = (255, 122, 40)
    text_main = (230, 232, 240)
    text_sub = (160, 165, 195)
    bar_a = (91, 141, 239)
    bar_b = (54, 201, 140)

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # Top accent bar
    d.rectangle((0, 0, W, 18), fill=accent)

    f_title = _font(56)
    f_huge = _font(160)
    f_big = _font(60)
    f_body = _font(40)
    f_small = _font(32)
    f_tiny = _font(28)

    # ---- Header ----
    title_text = "YouTube Shorts analytics"
    d.text((40, 50), title_text, font=f_title, fill=text_main)
    if campaign_title:
        d.text((40, 120), f"Campaign: {campaign_title[:60]}", font=f_body, fill=text_sub)

    # ---- Huge view count ----
    y = 220
    d.text((40, y), "Total views", font=f_body, fill=text_sub)
    d.text((40, y + 50), f"{snap.views:,}", font=f_huge, fill=accent)

    # ---- Stats row ----
    y2 = 510
    cards = [
        ("Likes", f"{snap.likes:,}"),
        ("Comments", f"{snap.comments:,}"),
        ("Watch (min)", f"{snap.watch_time_minutes:,.1f}"),
        ("Length", _fmt_duration(snap.duration_seconds)),
    ]
    cw = (W - 80 - 30) // 4
    for i, (label, value) in enumerate(cards):
        x = 40 + i * (cw + 10)
        d.rectangle((x, y2, x + cw, y2 + 170), fill=panel)
        d.text((x + 20, y2 + 22), label, font=f_small, fill=text_sub)
        d.text((x + 20, y2 + 70), value, font=f_big, fill=text_main)

    # ---- Top countries bar chart ----
    y3 = 730
    d.text((40, y3), "Top countries (views)", font=f_body, fill=text_main)
    top_countries = snap.top_countries(6)
    if top_countries:
        max_views = max(c[1] for c in top_countries) or 1
        cy = y3 + 80
        bar_h = 55
        gap = 18
        for code, views in top_countries:
            d.text((40, cy + 8), code, font=f_small, fill=text_main)
            d.rectangle((130, cy, 130 + 30 + 800, cy + bar_h), fill=panel)
            bar_w = int(800 * views / max_views)
            d.rectangle((130, cy, 130 + bar_w, cy + bar_h), fill=bar_a)
            d.text((130 + bar_w + 15, cy + 8), f"{views:,}", font=f_small, fill=text_main)
            cy += bar_h + gap
    else:
        d.text((40, y3 + 80), "(no country data yet — needs a few days of views)",
               font=f_small, fill=text_sub)

    # ---- Age + gender ----
    y4 = 1330
    d.text((40, y4), "Audience age × gender", font=f_body, fill=text_main)
    if snap.age_gender:
        # Aggregate male/female per age bucket
        by_age: dict[str, dict[str, float]] = {}
        for age, gender, pct in snap.age_gender:
            by_age.setdefault(age, {})[gender] = pct
        ages_order = ["age13-17", "age18-24", "age25-34", "age35-44",
                      "age45-54", "age55-64", "age65-"]
        ay = y4 + 80
        bar_h = 40
        gap = 12
        for age in ages_order:
            if age not in by_age:
                continue
            label = age.replace("age", "").replace("-", "–")
            male = by_age[age].get("male", 0.0)
            female = by_age[age].get("female", 0.0)
            d.text((40, ay + 4), label, font=f_small, fill=text_main)
            scale = 800 / max(by_age[age].values() or [1])
            mw = int(male * 12)
            fw = int(female * 12)
            d.rectangle((180, ay, 180 + mw, ay + bar_h), fill=bar_a)
            d.rectangle((180 + mw + 6, ay, 180 + mw + 6 + fw, ay + bar_h), fill=bar_b)
            d.text((180 + mw + 6 + fw + 10, ay + 4),
                   f"M {male:.1f}% · F {female:.1f}%",
                   font=f_tiny, fill=text_sub)
            ay += bar_h + gap
    else:
        d.text((40, y4 + 80),
               "(no age/gender data yet — YouTube needs ~100+ views)",
               font=f_small, fill=text_sub)

    # ---- Footer ----
    fy = H - 200
    d.rectangle((0, fy, W, H), fill=panel)
    if post_url:
        d.text((40, fy + 25), post_url[:80], font=f_small, fill=accent)
    if captured_at:
        d.text((40, fy + 75), f"Captured: {captured_at}", font=f_tiny, fill=text_sub)
    if snap.posted_at:
        d.text((40, fy + 115), f"Posted: {snap.posted_at[:19].replace('T', ' ')}",
               font=f_tiny, fill=text_sub)
    d.text((40, fy + 155), "clipfarmer · YouTube Data + Analytics APIs",
           font=f_tiny, fill=text_sub)

    # Bottom accent bar
    d.rectangle((0, H - 18, W, H), fill=accent)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path


def _font(size: int):
    for p in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _fmt_duration(sec: float) -> str:
    sec = int(sec or 0)
    if sec < 60:
        return f"{sec}s"
    return f"{sec // 60}:{sec % 60:02d}"
