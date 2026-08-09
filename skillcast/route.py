"""Playlist routes built from the same evidence as individual skills."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from .emit import slugify, write
from .extract import (ExtractionError, dedupe_commands, known_tool_commands,
                      probe, read_screen)
from .fetch import FetchError, _yt_dlp, download, looks_like_url
from .narrate import fetch_subtitles
from .synth import build_guide_skill, build_skill
from .verify import shellcheck, verify


class RouteError(RuntimeError):
    pass


EARLY_WORDS = re.compile(
    r"\b(?:intro|introduction|beginner|basics|fundamentals|getting started|overview)\b",
    re.I)
LATE_WORDS = re.compile(r"\b(?:advanced|pro tips|deep dive|masterclass)\b", re.I)


def _entry_title(entry):
    return str(entry.get("title") or entry.get("id") or "Untitled video").strip()


def general_first(entries):
    """Stable-sort playlist entries using a deliberately small title heuristic."""
    def score(entry):
        title = _entry_title(entry)
        return int(bool(LATE_WORDS.search(title))) - int(bool(EARLY_WORDS.search(title)))
    return sorted(list(entries), key=score)


def sort_entries(entries, mode="playlist"):
    if mode == "general-first":
        return general_first(entries)
    return list(entries)


def _entry_url(entry):
    value = entry.get("webpage_url") or entry.get("url")
    if value and (str(value).startswith("http://") or str(value).startswith("https://")):
        return value
    video_id = entry.get("id") or value
    if video_id:
        return "https://www.youtube.com/watch?v=%s" % video_id
    return ""


def parse_flat_playlist(value):
    """Normalize yt-dlp's flat-playlist JSON into a small entry list."""
    if isinstance(value, (str, Path)):
        if isinstance(value, str) and value.lstrip().startswith("{"):
            value = json.loads(value)
        else:
            value = json.loads(Path(value).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RouteError("yt-dlp returned no playlist information")
    entries = []
    for raw in value.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["title"] = _entry_title(item)
        item["url"] = _entry_url(item)
        if item["url"]:
            entries.append(item)
    if not entries:
        raise RouteError(
            "the playlist is empty or its videos are private. Make it public, "
            "or pass the videos as local files instead.")
    return {
        "title": str(value.get("title") or "Playlist").strip() or "Playlist",
        "entries": entries,
    }


def list_playlist(url, cookies_from_browser=None):
    if not looks_like_url(url):
        raise RouteError("route needs a playlist URL, not a local file")
    runner = _yt_dlp()
    if not runner:
        raise RouteError(
            "reading a playlist needs yt-dlp, which is not installed.\n"
            "  pip install yt-dlp\n"
            "  Or pass individual video files instead.")
    command = runner + ["--no-warnings", "--flat-playlist",
                        "--dump-single-json"]
    if cookies_from_browser:
        command += ["--cookies-from-browser", cookies_from_browser]
    try:
        result = subprocess.run(command + [url], capture_output=True, text=True,
                                timeout=120)
    except FileNotFoundError:
        raise RouteError("yt-dlp was not found on PATH. Install it and try again.")
    except subprocess.TimeoutExpired:
        raise RouteError("yt-dlp took too long to read the playlist. Check the URL and try again.")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        reason = detail[-1][:300] if detail else "yt-dlp failed"
        raise RouteError(
            "could not read the playlist: %s\n"
            "  Check that it is public and that yt-dlp can open it; private playlists "
            "need browser cookies." % reason)
    try:
        data = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise RouteError(
            "yt-dlp returned invalid playlist data. Update yt-dlp and try again.")
    return parse_flat_playlist(data)


def _clean_route_text(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    # Error text can come from internals; keep the route readable for a person.
    return (value.replace("OCR", "on-screen text")
            .replace("schema", "format")
            .replace("slug", "name"))


def format_duration(seconds):
    if seconds is None:
        return "unknown duration"
    try:
        seconds = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "unknown duration"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return "%dh %02dm" % (hours, minutes)
    if minutes:
        return "%dm %02ds" % (minutes, seconds)
    return "%ds" % seconds


def _first_chapter(chapters):
    if not chapters:
        return ""
    ordered = sorted(chapters, key=lambda item: float(item.get("start_time", item.get("start", 0)) or 0))
    for chapter in ordered:
        title = chapter.get("title") or ""
        # "<Untitled Chapter 1>" is YouTube's placeholder, not a real title.
        if title and not re.match(r"^<Untitled Chapter \d+>$", title.strip()):
            return _clean_route_text(title)
    return ""


def assemble_route(playlist_title, goal, videos, failures=None):
    """Render the end-user route document from processed video records."""
    failures = list(failures or [])
    heading = _clean_route_text(goal or playlist_title or "Learning route")
    out = ["# %s" % heading, "", "Goal: %s" % heading, "", "## Curriculum", ""]
    skill_paths = []
    cumulative = 0.0
    for index, video in enumerate(videos, 1):
        title = _clean_route_text(video.get("title") or "Video %d" % index)
        duration = video.get("duration_s")
        if duration is not None:
            try:
                cumulative += float(duration)
            except (TypeError, ValueError):
                pass
        duration_text = format_duration(duration)
        cumulative_text = format_duration(cumulative) if cumulative else "unknown duration"
        out.append("%d. **%s** — %s (cumulative %s)" %
                   (index, title, duration_text, cumulative_text))
        ability = _clean_route_text(video.get("ability") or title)
        out.append("   What you'll be able to do: %s" % ability)
        skill_path = _clean_route_text(video.get("skill_path") or "")
        if skill_path:
            skill_paths.append(skill_path)
            out.append("   Skill: [%s](%s)" % ("SKILL.md", skill_path))
        out.append("")

    out += ["## Videos not included", ""]
    if failures:
        for failure in failures:
            out.append("- **%s**: %s" %
                       (_clean_route_text(failure.get("title")),
                        _clean_route_text(failure.get("reason"))))
    else:
        out.append("None.")
    out += ["", "## Give this route to your agent", "",
            "```text",
            "Learn this route: %s in order." % ", ".join(skill_paths),
            "```", ""]
    return "\n".join(out)


def _ability(chapters, cues, title):
    return _first_chapter(chapters) or (cues[0].text if cues else title)


def _process_entry(entry, output_dir, index, narration_enabled=True,
                   cookies_from_browser=None):
    title = _entry_title(entry)
    video_url = _entry_url(entry)
    video_dir = Path(output_dir) / ("%02d-%s" % (index, slugify(title)))
    with tempfile.TemporaryDirectory(prefix="skillcast-route-") as tmp:
        video, info = download(video_url, dest_dir=tmp,
                               cookies_from_browser=cookies_from_browser)
        info = info or {}
        track = fetch_subtitles(video_url, video.parent,
                                cookies_from_browser=cookies_from_browser) if narration_enabled else None
        probe_info = probe(video)
        observations = read_screen(video)
        commands = dedupe_commands(observations)
        recognised = known_tool_commands(observations)
        chapters = info.get("chapters") or entry.get("chapters") or []
        # Same precedence as the single-video path: real commands beat
        # narration; narration beats OCR noise.
        if recognised:
            skill = build_skill(
                observations, source=title, name=slugify(title), title=title,
                narration=track.cues if track else None,
                narration_language=track.language if track else "",
            )
        elif track and track.cues:
            skill = build_guide_skill(
                observations, track.cues, source=title, name=slugify(title),
                title=title, chapters=chapters,
                duration_s=info.get("duration_s") or probe_info.get("duration_s"),
                narration_language=track.language,
            )
        elif commands:
            raise RouteError(
                "candidate commands were found, but none uses a recognised program")
        else:
            raise RouteError("no commands or narration were found")
        findings, _ = verify(skill)
        if skill.kind != "guide":
            findings += shellcheck(skill)
        errors = [finding for finding in findings if finding.level == "error"]
        if errors:
            raise RouteError("verification failed: %s" % errors[0].message)
        written = write(skill, video_dir)
        skill_path = next((path for path in written if path.name == "SKILL.md"), None)
        duration_s = info.get("duration_s") or probe_info.get("duration_s") or entry.get("duration")
        return {
            "title": title,
            "duration_s": duration_s,
            "ability": _ability(chapters, track.cues if track else [], title),
            "skill_path": str(skill_path.relative_to(output_dir)) if skill_path else "",
            "kind": skill.kind,
            "skill": skill,
        }


def run_route(url, output="route", goal="", sort_mode="playlist", limit=None,
              narration_enabled=True, dry_run=False, cookies_from_browser=None):
    playlist = list_playlist(url, cookies_from_browser=cookies_from_browser)
    entries = sort_entries(playlist["entries"], sort_mode)
    if limit is not None:
        entries = entries[:max(0, limit)]
    playlist_slug = slugify(playlist["title"], fallback="learning-route")
    root = Path(output) / playlist_slug
    if dry_run:
        return {
            "playlist": playlist["title"], "goal": goal or playlist["title"],
            "entries": entries, "videos": [], "failures": [],
            "path": str(root), "reordered": sort_mode == "general-first",
            "markdown": "",
        }
    root.mkdir(parents=True, exist_ok=True)
    videos, failures = [], []
    for index, entry in enumerate(entries, 1):
        try:
            videos.append(_process_entry(
                entry, root, index, narration_enabled=narration_enabled,
                cookies_from_browser=cookies_from_browser))
        except (FetchError, ExtractionError, RouteError, OSError, ValueError) as error:
            failures.append({"title": _entry_title(entry), "reason": str(error)})
    markdown = assemble_route(playlist["title"], goal, videos, failures)
    route_path = root / "ROUTE.md"
    route_path.write_text(markdown, encoding="utf-8")
    return {
        "playlist": playlist["title"], "goal": goal or playlist["title"],
        "entries": entries, "videos": videos, "failures": failures,
        "path": str(root), "route_path": str(route_path),
        "reordered": sort_mode == "general-first", "markdown": markdown,
    }
