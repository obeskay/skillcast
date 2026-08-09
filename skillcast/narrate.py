"""Read subtitle tracks without making narration sound more certain than it is.

Subtitles are useful evidence for intent and sequence, but they are not a
replacement for commands read from the screen. This module only cleans the
subtitle container syntax and keeps the words supplied by the video.
"""
from __future__ import annotations

import html
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .fetch import _yt_dlp


DEFAULT_SUB_LANGS = "en.*,en,all"
MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
TAG_RE = re.compile(r"<[^>]+>")
TIMING_RE = re.compile(r"^\s*(\S+)\s+-->\s+(\S+)(?:\s+.*)?$")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Cue:
    start_s: float
    end_s: float
    text: str

    def __getitem__(self, key):
        return getattr(self, key)


@dataclass
class NarrationTrack:
    cues: list
    language: str = ""
    path: str = ""
    source: str = ""

    @property
    def text(self):
        return " ".join(cue.text for cue in self.cues)


def dedupe_rolling(cues):
    """Undo YouTube's rolling auto-captions.

    Auto-generated cues repeat the previous cue's tail as their head, so a
    naive join prints every sentence twice or three times. Strip the longest
    word-level overlap between each cue and its predecessor. Manual subtitles
    have no overlap, so this is a no-op for them.
    """
    deduped = []
    prev_words = []
    for cue in cues:
        words = cue.text.split()
        overlap = 0
        for size in range(min(len(prev_words), len(words)), 0, -1):
            if prev_words[-size:] == words[:size]:
                overlap = size
                break
        fresh = words[overlap:]
        if fresh:
            deduped.append(Cue(cue.start_s, cue.end_s, " ".join(fresh)))
        prev_words = words
    return deduped


def parse_timestamp(value):
    """Parse a WebVTT/SRT timestamp into seconds."""
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError("invalid subtitle timestamp %r" % value)
    try:
        return float(hours) * 3600 + float(minutes) * 60 + float(seconds)
    except ValueError:
        raise ValueError("invalid subtitle timestamp %r" % value)


def _clean_text(lines):
    text = " ".join(line.strip() for line in lines if line.strip())
    # VTT voice/cue styling and SRT alignment markers are presentation, not
    # words. Keep entities such as ampersands readable after removing them.
    text = re.sub(r"\{\\[^}]+\}", "", text)
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_cues(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.upper() == "WEBVTT" or line.startswith("NOTE"):
            index += 1
            while line.startswith("NOTE") and index < len(lines) and lines[index].strip():
                index += 1
            continue
        match = TIMING_RE.match(line)
        if not match:
            # Cue identifiers are allowed before the timing line.
            if index + 1 < len(lines) and TIMING_RE.match(lines[index + 1].strip()):
                index += 1
                line = lines[index].strip()
                match = TIMING_RE.match(line)
            if not match:
                index += 1
                continue
        try:
            start_s = parse_timestamp(match.group(1))
            end_s = parse_timestamp(match.group(2))
        except ValueError:
            index += 1
            continue
        index += 1
        body = []
        while index < len(lines) and lines[index].strip():
            body.append(lines[index])
            index += 1
        cue_text = _clean_text(body)
        if cue_text and end_s >= start_s:
            cues.append(Cue(start_s, end_s, cue_text))
    return dedupe_rolling(cues)


def _subtitle_text(value):
    if isinstance(value, Path):
        return value.read_text(encoding="utf-8-sig", errors="replace")
    if isinstance(value, str) and "\n" not in value and "\r" not in value:
        path = Path(value)
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8-sig", errors="replace")
    return str(value)


def parse_vtt(value):
    """Parse VTT text or a VTT path into ``Cue`` objects."""
    return _parse_cues(_subtitle_text(value))


def parse_srt(value):
    """Parse SRT text or an SRT path into ``Cue`` objects."""
    return _parse_cues(_subtitle_text(value))


def parse_subtitles(value, suffix=""):
    """Parse either supported subtitle format."""
    path = value if isinstance(value, Path) else Path(value) if isinstance(value, str) and Path(value).exists() else None
    extension = (path.suffix if path else suffix).lower()
    return parse_srt(value) if extension == ".srt" else parse_vtt(value)


def align_cues(cues, scenes):
    """Return narration for each scene using half-open overlap windows.

    A scene may be a timestamp in seconds or an observation carrying an
    ``at_seconds``/``timestamp_s`` field. The last scene stays open through the
    end of the track because no later scene boundary exists.
    """
    scene_times = []
    for scene in scenes:
        if isinstance(scene, (int, float)):
            scene_times.append(float(scene))
        else:
            scene_times.append(_scene_time(scene))
    aligned = []
    for index, start_s in enumerate(scene_times):
        end_s = scene_times[index + 1] if index + 1 < len(scene_times) else float("inf")
        aligned.append([
            cue for cue in cues
            if cue.end_s > start_s and cue.start_s < end_s
        ])
    return aligned


def align_cues_to_scenes(cues, scenes):
    """Descriptive alias used by callers that want the operation named out."""
    return align_cues(cues, scenes)


def _scene_time(scene):
    if isinstance(scene, dict):
        for key in ("at_seconds", "timestamp_s", "start_s", "time_s", "timestamp"):
            if scene.get(key) is not None:
                return float(scene[key])
    for key in ("at_seconds", "timestamp_s", "start_s", "time_s", "timestamp"):
        value = getattr(scene, key, None)
        if value is not None:
            return float(value)
    return 0.0


def trim_narration(text, max_sentences=3, max_chars=900):
    """Keep a short, verbatim-light narration note."""
    cleaned = _clean_text(str(text).split("\n"))
    if not cleaned:
        return ""
    sentences = SENTENCE_END.split(cleaned)
    selected = " ".join(sentences[:max_sentences]).strip()
    if len(selected) > max_chars:
        selected = selected[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return selected


def cue_text(cues, max_sentences=3):
    return trim_narration(" ".join(cue.text for cue in cues), max_sentences=max_sentences)


SHORTCUT_RE = re.compile(r"\b(?:Ctrl|Cmd|Shift|Alt)\+[A-Z0-9]\b", re.I)
SPOKEN_RE = re.compile(r"\b(ctrl|cmd|command|shift|alt)\s+(?:and|plus)\s+([a-z0-9])\b", re.I)
PRESS_RE = re.compile(r"\bpress(?:ing|ed)?\s+(?:the\s+)?([A-Z0-9])\b", re.I)


def detect_shortcuts(text):
    """Extract explicit shortcuts, typed ('Ctrl+R') or spoken ('pressing Ctrl and R')."""
    found = []
    for match in SHORTCUT_RE.finditer(text or ""):
        modifier, key = match.group(0).split("+", 1)
        shortcut = modifier.capitalize() + "+" + key.upper()
        if shortcut not in found:
            found.append(shortcut)
    for match in SPOKEN_RE.finditer(text or ""):
        modifier = "Cmd" if match.group(1).lower() in ("cmd", "command") else match.group(1).capitalize()
        shortcut = modifier + "+" + match.group(2).upper()
        if shortcut not in found:
            found.append(shortcut)
    for match in PRESS_RE.finditer(text or ""):
        shortcut = match.group(1).upper()
        if shortcut not in found:
            found.append(shortcut)
    return found


def _language_for(path, fallback=""):
    parts = path.name.split(".")
    if len(parts) >= 3:
        candidate = parts[-2]
        if re.match(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?$", candidate):
            return candidate.replace("_", "-")
    return fallback or "unknown"


def _candidate_subtitles(directory):
    return sorted(p for p in Path(directory).iterdir()
                  if p.is_file() and p.suffix.lower() in (".vtt", ".srt")
                  and p.stat().st_size <= MAX_SUBTITLE_BYTES)


def _choose_subtitle(paths):
    if not paths:
        return None, ""
    def rank(path):
        language = _language_for(path).lower()
        if language == "en":
            language_rank = 0
        elif language.startswith("en-"):
            language_rank = 1
        else:
            language_rank = 2
        return language_rank, path.name
    chosen = sorted(paths, key=rank)[0]
    return chosen, _language_for(chosen)


def load_sidecar(video):
    """Load a subtitle next to a local video, if one exists."""
    video = Path(video)
    candidates = [video.with_suffix(".vtt"), video.with_suffix(".srt")]
    candidates += sorted(video.parent.glob(video.name + ".vtt"))
    candidates += sorted(video.parent.glob(video.name + ".srt"))
    candidates = [path for path in candidates if path.exists()]
    chosen, language = _choose_subtitle(candidates)
    if not chosen:
        return None
    cues = parse_subtitles(chosen)
    return NarrationTrack(cues, language, str(chosen), "sidecar") if cues else None


def fetch_subtitles(url, dest_dir, cookies_from_browser=None,
                    sub_langs=DEFAULT_SUB_LANGS):
    """Fetch manual subtitles first, then auto subtitles, without failing video extraction."""
    runner = _yt_dlp()
    if not runner:
        return None
    directory = Path(dest_dir)
    directory.mkdir(parents=True, exist_ok=True)
    template = str(directory / "%(id)s.%(ext)s")
    common = (runner + ["--no-warnings", "--no-playlist", "--skip-download",
                        "--sub-format", "vtt", "--sub-langs", sub_langs,
                        "-o", template])
    command = common + ["--write-subs", "--write-auto-subs"]
    if cookies_from_browser:
        command += ["--cookies-from-browser", cookies_from_browser]
    try:
        result = subprocess.run(command + [url], capture_output=True,
                                text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    candidates = _candidate_subtitles(directory)
    if result.returncode != 0 and not candidates:
        return None
    chosen, language = _choose_subtitle(candidates)
    if chosen:
        cues = parse_subtitles(chosen)
        if cues:
            # yt-dlp prefers manual subtitles when both flags are present. The
            # filename language ranking then prefers English over other tracks.
            return NarrationTrack(cues, language, str(chosen), "manual or auto")
    return None
