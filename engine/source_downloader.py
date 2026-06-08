"""Download a campaign's source footage from the link in its brief.

The brief_fetcher extracts source_links (WeTransfer, Google Drive file
links, etc.) into `campaigns.source_links`. This module turns those URLs
into actual local files so the orchestrator can run end-to-end without
any manual steps.

Supported handlers:
  - Direct video/file URLs (http(s) with mp4/mov/mkv/webm extension) → urllib
  - Google Drive single-file URLs (`/file/d/<id>` or `?id=<id>`)         → direct download endpoint with cookie dance
  - WeTransfer (`we.tl/...` or `wetransfer.com/downloads/...`)           → Playwright in the existing authed session, click "I agree" + "Download", monitor downloads folder

Anything we don't recognise returns None and the caller falls back to
asking the human for a local path (current default behaviour).

WeTransfer specifically can be GIGS — we don't try to parse the zip /
multi-file archive here. We just save whatever the host serves; the
orchestrator's downloader already accepts local paths and the engine
will try the file as-is. If the source is a multi-video zip, the user
unpacks it once and points the orchestrator at the .mp4 inside.
"""

from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings


# ----------------------------------------------------------------------
# Public entrypoint
# ----------------------------------------------------------------------
class SourceDownloadError(RuntimeError):
    pass


def download_source(url: str, dest_dir: Optional[Path] = None, page=None) -> Optional[Path]:
    """Attempt to download `url` and return the local path. None if unsupported.

    `page` is an optional Playwright Page already-authed against Whop — used
    for the WeTransfer handler so we don't spin up a second browser.
    """
    dest_dir = dest_dir or settings.download_dir
    dest_dir.mkdir(parents=True, exist_ok=True)

    handler = _pick_handler(url)
    if handler is None:
        logger.warning(f"[src-dl] no handler for {url}")
        return None
    logger.info(f"[src-dl] {handler.__name__} ← {url}")
    return handler(url, dest_dir, page)


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------
DIRECT_VIDEO_RE = re.compile(r"\.(mp4|mov|mkv|webm|m4v)(\?|$)", re.IGNORECASE)
GDRIVE_FILE_RE = re.compile(r"https://drive\.google\.com/file/d/([a-zA-Z0-9_-]+)", re.IGNORECASE)
GDRIVE_OPEN_RE = re.compile(r"https://drive\.google\.com/open\?(?:[^#]*&)?id=([a-zA-Z0-9_-]+)", re.IGNORECASE)
WETRANSFER_SHORT_RE = re.compile(r"https?://we\.tl/[^\s?#]+", re.IGNORECASE)
WETRANSFER_LONG_RE = re.compile(r"https?://wetransfer\.com/downloads/[^\s?#]+", re.IGNORECASE)


def _pick_handler(url: str):
    if GDRIVE_FILE_RE.search(url) or GDRIVE_OPEN_RE.search(url):
        return _download_gdrive
    if WETRANSFER_SHORT_RE.search(url) or WETRANSFER_LONG_RE.search(url):
        return _download_wetransfer
    if DIRECT_VIDEO_RE.search(url):
        return _download_direct
    return None


# ----- direct HTTP -----------------------------------------------------
def _download_direct(url: str, dest_dir: Path, page=None) -> Optional[Path]:
    fname = _safe_filename_from_url(url) or "source.mp4"
    out = dest_dir / fname
    return _stream_to_file(url, out)


# ----- Google Drive ----------------------------------------------------
def _download_gdrive(url: str, dest_dir: Path, page=None) -> Optional[Path]:
    """Best-effort download of a single-file shared Drive URL.

    Drive serves a confirmation HTML page for files over ~100 MB. The
    canonical workaround is a second GET that carries a confirmation
    cookie. We implement that minimal flow with urllib + http.cookiejar.
    """
    m = GDRIVE_FILE_RE.search(url) or GDRIVE_OPEN_RE.search(url)
    if not m:
        return None
    file_id = m.group(1)
    base = "https://drive.google.com/uc?export=download"
    initial = f"{base}&id={file_id}"

    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", _UA)]

    try:
        with opener.open(initial, timeout=60) as r1:
            content_type = r1.headers.get_content_type()
            disp = r1.headers.get("Content-Disposition", "")
            if content_type.startswith("text/html"):
                # Need confirmation token. Pull it from the page.
                html = r1.read().decode("utf-8", errors="ignore")
                token = _extract_gdrive_confirm_token(html, jar)
                if not token:
                    logger.warning("[src-dl] gdrive: no confirm token; file may be private")
                    return None
                final_url = f"{base}&confirm={token}&id={file_id}"
            else:
                final_url = initial
                # We already have the first chunk in r1 — but easier to re-issue.
        # Now do the actual download.
        fname = _filename_from_disposition(disp) or f"gdrive_{file_id}.mp4"
        out = dest_dir / fname
        return _stream_response(opener.open(final_url, timeout=120), out)
    except Exception as e:
        logger.warning(f"[src-dl] gdrive download failed: {e}")
        return None


def _extract_gdrive_confirm_token(html: str, jar) -> Optional[str]:
    # New Drive UX uses a form on the HTML page.
    m = re.search(r'name="confirm"\s+value="([^"]+)"', html)
    if m:
        return m.group(1)
    for cookie in jar:
        if cookie.name.startswith("download_warning"):
            return cookie.value
    m = re.search(r'confirm=([0-9A-Za-z_-]+)', html)
    return m.group(1) if m else None


# ----- WeTransfer ------------------------------------------------------
def _download_wetransfer(url: str, dest_dir: Path, page=None) -> Optional[Path]:
    """WeTransfer needs JS — we use Playwright in the existing Whop session.

    The strategy:
      1. Navigate to the URL.
      2. Accept the cookie / "I agree" banner if shown.
      3. Click "Download" / "Download all".
      4. Capture the resulting download via Playwright's `download` event
         and save it into our downloads dir.

    Returns the saved file's path, or None on failure.
    """
    if page is None:
        logger.warning("[src-dl] wetransfer requires a Playwright page; skipping")
        return None
    try:
        logger.info(f"[src-dl] wetransfer goto {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(5)
        # Dismiss any cookie / terms banner.
        for sel in (
            'button:has-text("I agree")',
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("Got it")',
        ):
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=1_500):
                    btn.click(timeout=3_000)
                    time.sleep(1)
            except Exception:
                continue

        # Trigger the actual download click.
        with page.expect_download(timeout=300_000) as dl_info:
            for sel in (
                'a:has-text("Download all")',
                'button:has-text("Download all")',
                'a:has-text("Download")',
                'button:has-text("Download")',
            ):
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible(timeout=2_000):
                        btn.click(timeout=5_000)
                        break
                except Exception:
                    continue
        download = dl_info.value
        suggested = download.suggested_filename or "wetransfer.zip"
        out = dest_dir / suggested
        download.save_as(str(out))
        logger.info(f"[src-dl] wetransfer saved {out} ({out.stat().st_size:,} bytes)")
        return out
    except Exception as e:
        logger.warning(f"[src-dl] wetransfer failed: {e}")
        return None


# ----------------------------------------------------------------------
# Plumbing
# ----------------------------------------------------------------------
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


def _stream_to_file(url: str, out_path: Path) -> Optional[Path]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        return _stream_response(urllib.request.urlopen(req, timeout=120), out_path)
    except Exception as e:
        logger.warning(f"[src-dl] direct fetch failed for {url}: {e}")
        return None


def _stream_response(resp, out_path: Path) -> Optional[Path]:
    total = int(resp.headers.get("Content-Length") or 0)
    written = 0
    chunk = 1 << 16
    next_log = time.time() + 5
    try:
        with open(out_path, "wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                written += len(buf)
                if time.time() >= next_log:
                    if total:
                        pct = written * 100 / total
                        logger.info(f"[src-dl] {pct:5.1f}% ({written/1_000_000:.1f}/{total/1_000_000:.1f} MB)")
                    else:
                        logger.info(f"[src-dl] {written/1_000_000:.1f} MB...")
                    next_log = time.time() + 5
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if written == 0:
        try:
            out_path.unlink()
        except Exception:
            pass
        return None
    logger.info(f"[src-dl] saved {out_path} ({written:,} bytes)")
    return out_path


def _safe_filename_from_url(url: str) -> Optional[str]:
    try:
        path = urllib.parse.urlparse(url).path
        name = Path(urllib.parse.unquote(path)).name
        if name and "." in name:
            return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:200]
    except Exception:
        pass
    return None


def _filename_from_disposition(disp: str) -> Optional[str]:
    if not disp:
        return None
    m = re.search(r'filename\*?="?([^";]+)"?', disp)
    if not m:
        return None
    name = urllib.parse.unquote(m.group(1).strip())
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:200] or None
