"""Accept a URL where a file path is expected.

Pasting a link is the obvious thing to try, so it should work. The download
itself is delegated to yt-dlp, which is the only sane way to keep up with how
video sites change; it stays an optional dependency so the core tool remains
dependency-free.

Nothing here touches the network unless the input actually looks like a URL.
"""
from __future__ import annotations

import re
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

URL_RE = re.compile(r"^https?://", re.I)

# Hosts yt-dlp handles that people plausibly point this at. The list is only
# used to give a better error message; yt-dlp itself decides what it supports.
FAMILIAR = ("youtube.com", "youtu.be", "vimeo.com", "loom.com", "twitch.tv",
            "asciinema.org", "dailymotion.com", "streamable.com")

# A tutorial worth turning into a skill is minutes long, not hours. The cap
# keeps a mistyped playlist link from filling the disk.
MAX_DURATION_S = 3 * 60 * 60
METADATA_TIMEOUT_S = 120
DOWNLOAD_TIMEOUT_S = 45 * 60


class FetchError(RuntimeError):
    pass


def looks_like_url(value):
    return bool(URL_RE.match(str(value).strip()))


def _yt_dlp():
    found = shutil.which("yt-dlp") or shutil.which("youtube-dl")
    return [found] if found else None


def probe_url(url):
    """Title and duration without downloading, so limits apply before the bytes."""
    runner = _yt_dlp()
    if not runner:
        return {}
    try:
        result = subprocess.run(
            runner + ["--no-warnings", "--no-playlist", "--skip-download",
                      "--dump-single-json", url],
            capture_output=True, text=True, timeout=METADATA_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout or "{}")
        if isinstance(data, dict):
            if data.get("duration") is not None:
                data["duration_s"] = float(data["duration"])
            return data
    except (TypeError, ValueError):
        pass
    return {}


# YouTube's bot checks reject anonymous downloads in waves. The documented way
# through is to reuse a real browser session, so the error says exactly that.
BOT_WALL = re.compile(
    r"(403|forbidden|needs to be reloaded|sign in to confirm|not a bot|"
    r"confirm your age|player API)", re.I)


def download(url, dest_dir=None, quality="best", on_progress=None,
             cookies_from_browser=None):
    """Fetch a video to a local file and return (path, info).

    Prefers a stream around 720p: OCR gains nothing from 4K, and the download
    is many times faster. Falls back to whatever is available.
    """
    runner = _yt_dlp()
    if not runner:
        raise FetchError(
            "reading a URL needs yt-dlp, which is not installed.\n"
            "  pip install yt-dlp\n"
            "  Or download the video yourself and pass the file path instead."
        )

    if not looks_like_url(url):
        raise FetchError("%r is not a URL" % url)

    info = probe_url(url)
    duration = info.get("duration_s")
    if duration and duration > MAX_DURATION_S:
        raise FetchError(
            "that video is %.1f hours long; the limit is %d hours. Trim it, or "
            "pass a local file." % (duration / 3600, MAX_DURATION_S // 3600)
        )

    target = Path(dest_dir or tempfile.mkdtemp(prefix="skillcast-dl-"))
    target.mkdir(parents=True, exist_ok=True)
    template = str(target / "%(id)s.%(ext)s")

    # 720p is the sweet spot: enough pixels for OCR, a fraction of the bytes.
    selector = ("bestvideo[height<=720]+bestaudio/best[height<=720]/best"
                if quality == "best" else quality)

    if on_progress:
        on_progress("downloading %s" % (info.get("title") or url))

    command = runner + ["--no-warnings", "--no-playlist", "-f", selector,
                        "--merge-output-format", "mp4", "-o", template]
    if cookies_from_browser:
        command += ["--cookies-from-browser", cookies_from_browser]
    try:
        result = subprocess.run(command + [url], capture_output=True, text=True,
                                timeout=DOWNLOAD_TIMEOUT_S)
    except FileNotFoundError:
        raise FetchError("yt-dlp was not found on PATH. Install it and try again.")
    except subprocess.TimeoutExpired:
        raise FetchError("yt-dlp took too long to download the video. Check the URL and try again.")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        message = detail[-1][:300] if detail else "yt-dlp failed"
        hint = ""
        if BOT_WALL.search(message) and not cookies_from_browser:
            hint = (
                "\n  This is a bot check, not a broken link — the site refused an "
                "anonymous download."
                "\n  Retry reusing your browser session:"
                "\n      skillcast \"%s\" --cookies-from-browser chrome"
                "\n  (also accepts firefox, safari, edge, brave)"
                "\n  Or download it yourself and pass the file path." % url)
        elif any(host in url for host in FAMILIAR):
            hint = ("\n  If the video is private, age-restricted or region-locked, "
                    "yt-dlp cannot reach it either.")
        raise FetchError("could not download %s: %s%s" % (url, message, hint))

    files = [p for p in sorted(target.iterdir()) if p.is_file()]
    if not files:
        raise FetchError("yt-dlp reported success but produced no file")
    # Largest file is the merged video; sidecars are small.
    video = max(files, key=lambda p: p.stat().st_size)
    return video, info
